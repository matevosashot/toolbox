#!/usr/bin/env python3
"""
Telegram remote shell server.

Polls a Telegram channel for messages that start with a command prefix
(default: /k1012), executes them as shell commands on the local machine,
and sends stdout/stderr back to the channel.

Usage:
    python teleserver.py [--chat_id log] [--token <TOKEN>] [--prefix /k1012]
                         [--poll_interval 1] [--timeout 30] [--log_path ~/logs/teleserver/]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import time

import requests

from toolbox.configs.telegram import CHANNEL_IDS, TELEGRAM_BOT_TOKEN

log = logging.getLogger(__name__)


def _setup_logging(log_path: str) -> None:
    log_path = os.path.expanduser(log_path)
    if not log_path.endswith(".log"):
        os.makedirs(log_path, exist_ok=True)
        log_path = os.path.join(log_path, "teleserver.log")
    else:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_path, mode="a")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    log.setLevel(logging.DEBUG)
    log.addHandler(file_handler)
    log.addHandler(stream_handler)

# Commands whose leading token matches any of these patterns require confirmation.
SENSITIVE_PATTERNS: list[str] = [
    r"rm\b",
    r"dd\b",
    r"mkfs\b",
    r"shred\b",
    r"truncate\b",
    r"fdisk\b",
    r"parted\b",
    r"shutdown\b",
    r"reboot\b",
    r"halt\b",
    r"poweroff\b",
    r"kill\b",
    r"killall\b",
    r"pkill\b",
    r"chmod\b",
    r"chown\b",
    r"mv\b",
    r">\s*/",          # redirect into absolute path (e.g. > /etc/...)
    r"sudo\b",
]
_SENSITIVE_RE = re.compile("|".join(SENSITIVE_PATTERNS))

# How long (seconds) a pending confirmation stays valid.
CONFIRMATION_TTL = 60


class TelegramShellServer:
    """Polls a Telegram channel for prefixed messages and runs them as shell commands."""

    API_BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(
        self,
        chat_id: str,
        token: str = TELEGRAM_BOT_TOKEN,
        prefix: str = "$",
        poll_interval: float = 1.0,
        timeout: int = 30,
    ) -> None:
        self.chat_id = str(CHANNEL_IDS.get(chat_id, chat_id))
        self.token = token
        self.prefix = prefix
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._offset = 0
        # At most one command awaiting confirmation at a time.
        self._pending_command: str | None = None
        self._pending_at: float = 0.0

    # ------------------------------------------------------------------
    # Telegram API
    # ------------------------------------------------------------------

    def _url(self, method: str) -> str:
        return self.API_BASE.format(token=self.token, method=method)

    def send_message(self, text: str) -> None:
        payload = {
            "chat_id": self.chat_id,
            "parse_mode": "MarkdownV2",
            "text": f"`{text[:4000]}`",  # Telegram message limit
        }
        try:
            resp = requests.post(self._url("sendMessage"), data=payload)
            if resp.status_code != 200:
                log.error("Failed to send message: %s", resp.text)
        except Exception as exc:
            log.error("Error sending message: %s", exc)

    def fetch_updates(self) -> list[dict]:
        resp = requests.get(self._url("getUpdates"), params={"offset": self._offset})
        if resp.status_code != 200:
            log.error("Error fetching updates: %s - %s", resp.status_code, resp.text)
            return []
        updates = resp.json().get("result", [])
        if updates:
            self._offset = max(upd["update_id"] + 1 for upd in updates)
        return updates

    # ------------------------------------------------------------------
    # Update parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_post(update: dict) -> dict | None:
        return (
            update.get("edited_channel_post")
            or update.get("channel_post")
        )

    def _is_relevant(self, update: dict) -> bool:
        post = self._extract_post(update)
        if post is None:
            return False
        if str(post.get("chat", {}).get("id", "")) != self.chat_id:
            return False
        return post.get("text", "").startswith(self.prefix)

    def _parse_command(self, update: dict) -> str:
        text = self._extract_post(update)["text"]
        return text[len(self.prefix):].lstrip("_").strip()

    # ------------------------------------------------------------------
    # Sensitive-command guard
    # ------------------------------------------------------------------

    @staticmethod
    def _is_sensitive(command: str) -> bool:
        return bool(_SENSITIVE_RE.search(command))

    def _pending_expired(self) -> bool:
        return time.monotonic() - self._pending_at > CONFIRMATION_TTL

    def _request_confirmation(self, command: str) -> None:
        if self._pending_command and self._pending_command != command and not self._pending_expired():
            self.send_message(
                f"Discarding pending command: {self.prefix} {self._pending_command}\n"
                f"Now awaiting confirmation for:\n"
                f"{self.prefix} {command}"
            )
        else:
            self.send_message(
                f"WARNING: sensitive command detected.\n"
                f"Send again to confirm ({CONFIRMATION_TTL}s window):\n"
                f"{self.prefix} {command}"
            )
        self._pending_command = command
        self._pending_at = time.monotonic()
        log.warning("Awaiting confirmation for: %r", command)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run_command(self, command: str) -> str:
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=self.timeout
            )
            return result.stdout if result.returncode == 0 else result.stderr
        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.timeout}s"
        except Exception as exc:
            return f"Error running command: {exc}"

    def _cancel_pending(self) -> None:
        log.info("Cancelled pending: %r", self._pending_command)
        self._pending_command = None
        self._pending_at = 0.0

    def handle_update(self, update: dict) -> None:
        if not self._is_relevant(update):
            return
        command = self._parse_command(update)

        # Any command other than the exact pending confirmation cancels it.
        if self._pending_command and not self._pending_expired():
            if command == self._pending_command:
                self._pending_command = None
                log.info("Confirmed, running: %r", command)
            else:
                self._cancel_pending()
                if self._is_sensitive(command):
                    self._request_confirmation(command)
                    return
        elif self._is_sensitive(command):
            self._request_confirmation(command)
            return

        log.info("Running: %r", command)
        output = self.run_command(command)
        self.send_message(output or "(no output)")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("Listening on chat %s for prefix %r ...", self.chat_id, self.prefix)
        while True:
            try:
                for update in self.fetch_updates():
                    try:
                        self.handle_update(update)
                    except Exception:
                        log.exception("Unhandled error in handle_update")
            except Exception:
                log.exception("Unhandled error in polling loop")
            time.sleep(self.poll_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telegram remote shell — executes shell commands received via a Telegram channel.",
    )
    parser.add_argument(
        "--chat_id",
        type=str,
        default="log",
        help=(
            f"Telegram chat/channel to listen on. "
            f"Named shortcuts: {list(CHANNEL_IDS.keys())}. "
            f"Or pass a raw numeric ID. (default: log)"
        ),
    )
    parser.add_argument(
        "--token",
        type=str,
        default=TELEGRAM_BOT_TOKEN,
        help="Telegram bot token. Falls back to $TELEGRAM_BOT_TOKEN env var.",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="$",
        help="Command prefix to listen for (default: $). Supports both '$cmd' and '$ cmd'.",
    )
    parser.add_argument(
        "--poll_interval",
        type=float,
        default=1.0,
        help="Seconds between polling cycles (default: 1).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Shell command timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--log_path",
        type=str,
        default="~/logs/",
        help="Directory (or .log file path) for log output (default: ~/logs/).",
    )

    args = parser.parse_args()

    if not args.token:
        parser.error("No token provided. Set --token or $TELEGRAM_BOT_TOKEN.")

    _setup_logging(args.log_path)

    TelegramShellServer(
        chat_id=args.chat_id,
        token=args.token,
        prefix=args.prefix,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    ).run()


if __name__ == "__main__":
    main()

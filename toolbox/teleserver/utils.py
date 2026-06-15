"""Shared helpers for teleserver extensions."""

from __future__ import annotations

import logging
import os
import re
import signal
import tempfile
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from toolbox.configs.telegram import resolve_chat_target

log = logging.getLogger("main")

TG_MESSAGE_LIMIT = 4000
TAIL_LINES = 10
TWO_DAYS_SECONDS = 2 * 24 * 60 * 60

# ANSI CSI + OSC sequences. Used to strip terminal escape codes from log files
# captured via PTY (e.g. tmux pipe-pane output).
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")

# Commands whose leading token matches any of these patterns are considered
# destructive enough to require explicit confirmation in the shell extension.
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
    r">\s*/",
    r"sudo\b",
]
SENSITIVE_RE = re.compile("|".join(SENSITIVE_PATTERNS))


def default_log_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "teleserver")


def is_sensitive(command: str) -> bool:
    return bool(SENSITIVE_RE.search(command))


def kill_pgid(pgid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except Exception:
        log.exception("killpg(%d) failed", pgid)


def read_tail(path: str, lines: int, read_window: int = 64 * 1024) -> str:
    """Return the last `lines` lines of `path` without loading the whole file."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            read_size = min(size, read_window)
            f.seek(size - read_size)
            data = f.read(read_size)
    except FileNotFoundError:
        return "(log unavailable)"
    except Exception:
        log.exception("Failed to read tail of %s", path)
        return "(tail error)"
    text = data.decode(errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def message_thread_ok(message, chat_id) -> bool:
    """Return True if `message` belongs to the thread encoded in `chat_id`.

    `chat_id` may be a combined ``<chat>:<thread>`` token; when it carries no
    thread (or is None) every message passes.
    """
    if chat_id is None:
        return True
    _, thread_id = resolve_chat_target(chat_id)
    if thread_id is None:
        return True
    return getattr(message, "message_thread_id", None) == thread_id


def parse_slashverb(text: str, verb: str, prefix: str) -> Optional[str]:
    """Strip a leading `/verb` (with optional `@botname`) and `prefix`.

    `/process $ ls -la` with verb=process, prefix=$ → "ls -la".
    Returns None if the prefix marker is missing.
    """
    m = re.match(rf"^/{re.escape(verb)}(?:@\S+)?\s*", text)
    if not m:
        return None
    rest = text[m.end():].lstrip()
    if prefix and not rest.startswith(prefix):
        return None
    return rest[len(prefix):].lstrip("_").strip()


async def send_message(bot: Bot, chat_id, text: str, **kwargs):
    """`bot.send_message` that understands a combined ``<chat>:<thread>`` id.

    The chat id is split here — the single point where a thread is needed — so
    callers can keep passing a single ``chat_id`` value around.
    """
    cid, thread_id = resolve_chat_target(chat_id)
    return await bot.send_message(
        chat_id=cid, text=text, message_thread_id=thread_id, **kwargs
    )


async def send_code_block(
    bot: Bot,
    chat_id,
    text: str,
    limit: int = TG_MESSAGE_LIMIT,
) -> None:
    """Send a MarkdownV2 single-backtick message — caller-friendly wrapper.

    Accepts a combined ``<chat>:<thread>`` ``chat_id`` and suppresses (but logs)
    send errors so callers can fire-and-forget.
    """
    cid, thread_id = resolve_chat_target(chat_id)
    try:
        await bot.send_message(
            chat_id=cid,
            text=f"`{(text or '(empty)')[:limit]}`",
            parse_mode=ParseMode.MARKDOWN_V2,
            message_thread_id=thread_id,
        )
    except Exception:
        log.exception("Failed to send code-block message")

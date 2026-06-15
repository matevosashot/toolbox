#!/usr/bin/env python3
"""Teleserver CLI: wires the shell/process/tmux extensions onto one Application.

This is a thin orchestrator. The feature implementations live in their own
modules (:mod:`toolbox.teleserver.shell`, :mod:`.process`, :mod:`.tmux`) and
can be reused independently from any python-telegram-bot Application.

Updates are pulled by :class:`~toolbox.teleserver.poller.Poller` and fed
into the Application's update queue manually so multiple processes can run
against the same bot, each scoped to a different chat.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from telegram import Bot, Update
from telegram.ext import Application, ApplicationBuilder

from toolbox import setup_loggers
from toolbox.configs.telegram import CHANNEL_IDS, TELEGRAM_BOT_TOKEN, resolve_chat_target

from .claude import ClaudeExtension
from .poller import Poller, RandomIntervalPoller
from .process import ProcessExtension
from .shell import ShellExtension
from .tmux import TmuxExtension
from .utils import message_thread_ok

log = logging.getLogger("main")


class ShellApp:
    """Composes shell + process + tmux extensions for a single chat."""

    def __init__(
        self,
        token: str,
        chat_id,
        prefix: str = "$",
        timeout: int = 30,
    ) -> None:
        # `chat_id` may be a combined "<chat>:<thread>" token; it is carried
        # around as-is and only split where a thread is actually needed.
        self.chat_id = chat_id
        self.prefix = prefix
        self.timeout = timeout

        self.application: Application = (
            ApplicationBuilder().token(token).updater(None).build()
        )
        self.shell = ShellExtension(
            self.application, chat_id=self.chat_id, prefix=prefix, timeout=timeout,
        )
        self.process = ProcessExtension(
            self.application, chat_id=self.chat_id, prefix=prefix,
        ).add_command_handler()
        self.tmux = TmuxExtension(
            self.application, chat_id=self.chat_id, prefix=prefix,
        ).add_command_handler()
        self.claude = ClaudeExtension(
            self.application, chat_id=self.chat_id, prefix=prefix,
        ).add_command_handler()

    @property
    def bot(self) -> Bot:
        return self.application.bot

    async def start(self) -> None:
        await self.application.initialize()
        await self.application.start()

    async def stop(self) -> None:
        await self.application.stop()
        await self.application.shutdown()

    async def feed(self, update: Update) -> None:
        chat = update.effective_chat
        target_chat, _ = resolve_chat_target(self.chat_id)
        if chat is None or chat.id != target_chat:
            return
        if not message_thread_ok(update.effective_message, self.chat_id):
            return
        await self.application.update_queue.put(update)


async def _run(app: ShellApp, poller: Poller, poll_interval: float) -> None:
    log.info("Listening on chat %s for prefix %r ...", app.chat_id, app.prefix)
    await app.start()
    try:
        while True:
            try:
                for update in await poller.pull():
                    await app.feed(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unhandled error in polling loop")
            await asyncio.sleep(poll_interval)
    finally:
        await app.stop()


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
            f"Or pass a raw numeric ID. Append ':<thread_id>' to scope to a "
            f"topic, e.g. 'log:42'. (default: log)"
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

    setup_loggers(base_path=args.log_path, stdout=True, train_logger=False)

    app = ShellApp(
        token=args.token,
        chat_id=args.chat_id,
        prefix=args.prefix,
        timeout=args.timeout,
    )
    # poller = Poller(app.bot)
    poller = RandomIntervalPoller(app.bot, max_buffer=5) 
    try:
        asyncio.run(_run(app, poller, args.poll_interval))
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down.")


if __name__ == "__main__":
    main()

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
import signal

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

        target_chat, thread_id = resolve_chat_target(chat_id)
        log.debug(
            "ShellApp init: chat_id=%r resolved to chat=%s thread=%s prefix=%r timeout=%s",
            chat_id, target_chat, thread_id, prefix, timeout,
        )

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

    async def shutdown(self) -> None:
        """Force-kill every spawned job, then stop the Application.

        Ensures detached ``/process`` commands and ``/tmux``/``/claude``
        sessions do not outlive the server on shutdown.
        """
        for ext in (self.process, self.tmux, self.claude):
            try:
                await ext.shutdown()
            except Exception:
                log.exception("Error shutting down /%s extension", ext.verb)
        await self.stop()

    async def feed(self, update: Update) -> None:
        chat = update.effective_chat
        target_chat, _ = resolve_chat_target(self.chat_id)
        incoming = None if chat is None else chat.id
        if chat is None or chat.id != target_chat:
            log.debug(
                "feed[%s]: drop update %s — chat %s != target %s",
                self.chat_id, update.update_id, incoming, target_chat,
            )
            return
        if not message_thread_ok(update.effective_message, self.chat_id):
            msg = update.effective_message
            log.debug(
                "feed[%s]: drop update %s — thread %s not in scope",
                self.chat_id, update.update_id,
                getattr(msg, "message_thread_id", None),
            )
            return
        log.debug(
            "feed[%s]: queue update %s from chat %s",
            self.chat_id, update.update_id, incoming,
        )
        await self.application.update_queue.put(update)


async def _amain(args, chat_ids: list[str]) -> None:
    """Build apps + poller and run the loop.

    Everything asyncio-bound (each Application's ``update_queue``, locks, the
    bot's HTTP client) MUST be constructed *inside* the running event loop.
    On Python 3.8 an ``asyncio.Queue`` binds to the loop returned by
    ``get_event_loop()`` at construction time; building the apps before
    ``asyncio.run()`` would bind their queues to a different loop, so
    ``update_queue.put`` and the Application's ``update_queue.get`` fetcher
    would run on separate loops — updates get queued but never dispatched.
    (Python 3.10+ binds lazily to the running loop and happens to work, which
    is why this only manifested under python3.8.)
    """
    apps = [
        ShellApp(
            token=args.token,
            chat_id=chat_id,
            prefix=args.prefix,
            timeout=args.timeout,
        )
        for chat_id in chat_ids
    ]

    if args.poller == "simple":
        poller: Poller = Poller(apps[0].bot)
    else:
        poller = RandomIntervalPoller(apps[0].bot, max_buffer=5)

    # Run the poll loop as a cancellable task so a termination signal
    # (SIGTERM from `systemctl stop`/`restart`, or SIGINT from Ctrl-C) cancels
    # it cleanly and lets `_run`'s `finally` force-kill spawned jobs. Without a
    # handler, the default SIGTERM disposition kills the process outright,
    # skipping cleanup and orphaning detached jobs / tmux sessions.
    run_task = asyncio.ensure_future(_run(apps, poller, args.poll_interval))
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, run_task.cancel)
        except (NotImplementedError, RuntimeError):
            # Signal handlers are unavailable on some platforms/loops; the
            # KeyboardInterrupt fallback in main() still covers Ctrl-C.
            pass

    try:
        await run_task
    except asyncio.CancelledError:
        log.info("Termination signal received, shut down cleanly.")


async def _run(apps: list[ShellApp], poller: Poller, poll_interval: float) -> None:
    log.info(
        "Listening on chats %s ...",
        [f"{a.chat_id} (prefix {a.prefix!r})" for a in apps],
    )
    for app in apps:
        await app.start()
    try:
        while True:
            try:
                updates = await poller.pull()
                if updates:
                    log.debug("Polled %d update(s): %s", len(updates),
                              [u.update_id for u in updates])
                for update in updates:
                    log.debug("Update %s: %s", update.update_id, update)
                    for app in apps:
                        await app.feed(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unhandled error in polling loop")
            await asyncio.sleep(poll_interval)
    finally:
        log.info("Shutting down: force-killing spawned jobs and tmux sessions ...")
        for app in apps:
            await app.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telegram remote shell — executes shell commands received via a Telegram channel.",
    )
    parser.add_argument(
        "--chat_id",
        type=str,
        action="append",
        default=None,
        help=(
            f"Telegram chat/channel(s) to listen on. "
            f"Named shortcuts: {list(CHANNEL_IDS.keys())}. "
            f"Or pass a raw numeric ID. Append ':<thread_id>' to scope to a "
            f"topic, e.g. 'log:42'. Repeat the flag to run the identical app "
            f"on multiple chats, e.g. '--chat_id log --chat_id dev:42'. "
            f"(default: log)"
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
        "--poller",
        type=str,
        choices=["simple", "random"],
        default="simple",
        help=(
            "Poller implementation: 'simple' (Poller) or 'random' "
            "(RandomIntervalPoller, jittered polling). (default: simple)"
        ),
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging (verbose polling/feed logs + ~/logs/debug.log).",
    )

    args = parser.parse_args()

    if not args.token:
        parser.error("No token provided. Set --token or $TELEGRAM_BOT_TOKEN.")

    setup_loggers(
        base_path=args.log_path, debug=args.debug, stdout=True, train_logger=False,
    )
    log.debug("Args: %s", vars(args))

    raw_chat_ids = args.chat_id if args.chat_id is not None else ["log"]
    chat_ids = [c.strip() for c in raw_chat_ids if c.strip()]

    try:
        asyncio.run(_amain(args, chat_ids))
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down.")


if __name__ == "__main__":
    main()

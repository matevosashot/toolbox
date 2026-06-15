"""`$ <cmd>` shell extension: prefix-triggered shell execution with confirmation.

Polls for inline-prefix messages (e.g. ``$ ls``), runs them as foreground
subprocesses with a timeout, and sends the captured output back. Destructive
commands (rm, sudo, …) require sending the exact same line twice within a
short confirmation window.

Usage:

    from telegram.ext import ApplicationBuilder
    from toolbox.teleserver import ShellExtension

    app = ApplicationBuilder().token(TOKEN).build()
    ShellExtension(app, chat_id=MY_CHAT, prefix="$", timeout=30)
    app.run_polling()
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from toolbox.configs.telegram import resolve_chat_target

from .utils import (
    is_sensitive,
    message_thread_ok,
    send_code_block,
)

log = logging.getLogger("main")

CONFIRMATION_TTL = 60


class ShellExtension:
    """Registers a prefix-triggered shell handler (``$ <cmd>`` by default).

    Args:
        application: a `telegram.ext.Application` to attach handlers to.
        chat_id: limit the handler to this chat. Pass ``None`` to allow any chat.
        prefix: prefix that marks a shell command (default ``"$"``).
        timeout: maximum seconds the subprocess may run before being killed.
        confirmation_ttl: seconds within which a destructive command must be
            repeated to confirm.
    """

    def __init__(
        self,
        application: Application,
        *,
        chat_id=None,
        prefix: str = "$",
        timeout: int = 30,
        confirmation_ttl: float = CONFIRMATION_TTL,
    ) -> None:
        self.app = application
        # `chat_id` may be a combined "<chat>:<thread>" token; carried as-is and
        # only split where a thread is actually needed (sends / filtering).
        self.chat_id = chat_id
        self.prefix = prefix
        self.timeout = timeout
        self.confirmation_ttl = confirmation_ttl

        self._pending_command: Optional[str] = None
        self._pending_at: float = 0.0

        chat_filter = (
            filters.Chat(chat_id=resolve_chat_target(chat_id)[0])
            if chat_id is not None
            else filters.ALL
        )
        application.add_handler(
            MessageHandler(
                chat_filter
                & filters.TEXT
                & filters.Regex(rf"^{re.escape(prefix)}"),
                self._on_message,
            )
        )

    @property
    def bot(self):
        return self.app.bot

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse_command(self, text: str) -> str:
        return text[len(self.prefix):].lstrip("_").strip()

    def _pending_expired(self) -> bool:
        return time.monotonic() - self._pending_at > self.confirmation_ttl

    def _cancel_pending(self) -> None:
        log.info("Cancelled pending: %r", self._pending_command)
        self._pending_command = None
        self._pending_at = 0.0

    async def _request_confirmation(self, chat_id, command: str) -> None:
        if (
            self._pending_command
            and self._pending_command != command
            and not self._pending_expired()
        ):
            text = (
                f"Discarding pending command: {self.prefix} {self._pending_command}\n"
                f"Now awaiting confirmation for:\n"
                f"{self.prefix} {command}"
            )
        else:
            text = (
                f"WARNING: sensitive command detected.\n"
                f"Send again to confirm ({int(self.confirmation_ttl)}s window):\n"
                f"{self.prefix} {command}"
            )
        await send_code_block(self.bot, chat_id, text)
        self._pending_command = command
        self._pending_at = time.monotonic()
        log.warning("Awaiting confirmation for: %r", command)

    async def _run_command(self, command: str) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return f"Error running command: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            return f"Command timed out after {self.timeout}s"

        if proc.returncode == 0:
            return stdout.decode(errors="replace")
        return stderr.decode(errors="replace") or stdout.decode(errors="replace")

    async def _on_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return
        if not message_thread_ok(message, self.chat_id):
            return
        command = self._parse_command(message.text)

        # Reply to the configured target (which carries the thread), falling
        # back to the originating chat when no chat is configured (any-chat mode).
        target = self.chat_id if self.chat_id is not None else chat.id

        if self._pending_command and not self._pending_expired():
            if command == self._pending_command:
                self._pending_command = None
                log.info("Confirmed, running: %r", command)
            else:
                self._cancel_pending()
                if is_sensitive(command):
                    await self._request_confirmation(target, command)
                    return
        elif is_sensitive(command):
            await self._request_confirmation(target, command)
            return

        log.info("Running: %r", command)
        output = await self._run_command(command)
        await send_code_block(self.bot, target, output or "(no output)")

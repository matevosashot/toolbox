"""Abstract base for ``/verb`` extensions that spawn detached jobs with
Kill/Tail buttons and an auto-kill timeout.

`ProcessExtension`, `TmuxExtension`, and `ClaudeExtension` share the same
lifecycle: parse the inbound message → guard → spawn → reply with a
"running" message + inline keyboard → watch for exit (or kill after a
timeout) → edit the message. Subclasses implement the parts that actually
differ.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generic, Optional, TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from toolbox.configs.telegram import resolve_chat_target

from .utils import (
    TWO_DAYS_SECONDS,
    default_log_dir,
    is_sensitive,
    message_thread_ok,
    parse_slashverb,
    send_code_block,
    send_message,
)

log = logging.getLogger("main")


@dataclass
class JobRecord:
    id: str
    cmd: str
    chat_id: int
    log_path: str
    message_id: int = 0
    started_at: float = 0.0
    watcher: Optional[asyncio.Task] = field(default=None)
    final_status: Optional[str] = field(default=None)
    metadata: Optional[dict] = field(default=None)

R = TypeVar("R", bound=JobRecord)


class JobExtension(ABC, Generic[R]):
    """Common skeleton for `/verb [prefix] <cmd>` → spawned-job extensions.

    Subclass attributes:
        verb: slash-command name (e.g. ``"process"``).
        callback_prefix: namespace string used in inline-button callback_data.
        id_pattern: regex matched against the ID portion of callback_data —
            tightens the per-extension `CallbackQueryHandler` so foreign
            buttons don't reach this instance.

    Subclasses must implement: `_spawn`, `_is_alive`, `_wait_for_exit`,
    `_kill`, `_tail`, `_status_text`. They may also override `_parse_input`
    and `_usage` for non-default message shapes.
    """

    verb: str = ""
    callback_prefix: str = ""
    id_pattern: str = r"\S+"

    def __init__(
        self,
        application: Application,
        *,
        chat_id=None,
        prefix: str = "$",
        log_dir: Optional[str] = None,
        auto_kill_seconds: float = TWO_DAYS_SECONDS,
        reject_sensitive: bool = True,
        verb: Optional[str] = None,
        callback_prefix: Optional[str] = None,
    ) -> None:
        self.app = application
        # `chat_id` may be a combined "<chat>:<thread>" token; carried as-is and
        # only split where a thread is actually needed (sends / filtering).
        self.chat_id = chat_id
        self.prefix = prefix
        self.log_dir = log_dir or default_log_dir()
        self.auto_kill_seconds = auto_kill_seconds
        self.reject_sensitive = reject_sensitive
        if verb is not None:
            self.verb = verb
        if callback_prefix is not None:
            self.callback_prefix = callback_prefix
        if not self.verb or not self.callback_prefix:
            raise ValueError("verb and callback_prefix must be set")

        self._kill_action = f"{self.callback_prefix}kill"
        self._tail_action = f"{self.callback_prefix}tail"
        self._records: dict[str, R] = {}

        self._chat_filter = (
            filters.Chat(chat_id=resolve_chat_target(chat_id)[0])
            if chat_id is not None
            else None
        )
        application.add_handler(
            CommandHandler(self.verb, self._on_command, filters=self._chat_filter)
        )
        application.add_handler(
            CallbackQueryHandler(
                self._on_callback,
                pattern=rf"^({self._kill_action}|{self._tail_action}):{self.id_pattern}$",
            )
        )

    def add_command_handler(self):
        self.app.add_handler(
            CommandHandler(self.verb, self._on_command, filters=self._chat_filter)
        )
        return self

    
    @property
    def bot(self):
        return self.app.bot

    # ------------------------------------------------------------------
    # Override hooks for parsing / usage
    # ------------------------------------------------------------------

    def _parse_input(self, text: str) -> Optional[str]:
        return parse_slashverb(text, self.verb, self.prefix)

    def _usage(self) -> str:
        usage_prefix = f"{self.prefix} " if self.prefix else ""
        return f"Usage: /{self.verb} {usage_prefix}<shell command>"

    # ------------------------------------------------------------------
    # Abstract — subclasses provide the actual behaviour
    # ------------------------------------------------------------------

    @abstractmethod
    async def _spawn(self, command: str, chat_id: int) -> R: ...

    @abstractmethod
    async def _is_alive(self, rec: R) -> bool: ...

    @abstractmethod
    async def _wait_for_exit(self, rec: R) -> str:
        """Block until the job ends (naturally or after ``auto_kill_seconds``)
        and return a final status string."""

    @abstractmethod
    async def _kill(self, rec: R) -> None: ...

    @abstractmethod
    async def _tail(self, rec: R) -> str: ...

    @abstractmethod
    def _status_text(self, rec: R, status: str) -> str: ...

    # ------------------------------------------------------------------
    # Shared lifecycle
    # ------------------------------------------------------------------

    def _keyboard(self, job_id: str, alive: bool) -> InlineKeyboardMarkup:
        buttons: list[InlineKeyboardButton] = []
        if alive:
            buttons.append(
                InlineKeyboardButton("Kill", callback_data=f"{self._kill_action}:{job_id}")
            )
        buttons.append(
            InlineKeyboardButton("Tail", callback_data=f"{self._tail_action}:{job_id}")
        )
        return InlineKeyboardMarkup([buttons])

    async def _on_command(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Telegram handler: parse the update, run user-facing guards, then
        delegate to :meth:`_start_job`. Anything that wants to launch a job
        *without* going through a /verb message should call `_start_job`
        directly."""
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return
        if not message_thread_ok(message, self.chat_id):
            return

        # Reply to the configured target (which carries the thread), falling
        # back to the originating chat when no chat is configured (any-chat mode).
        target = self.chat_id if self.chat_id is not None else chat.id

        command = self._parse_input(message.text)
        if not command:
            await send_message(self.bot, target, self._usage())
            return

        if self.reject_sensitive and is_sensitive(command):
            await send_message(
                self.bot, target,
                f"Sensitive commands are not supported via /{self.verb}.",
            )
            return

        await self._start_job(command, target, metadata={"message_text": message.text})

    async def _start_job(self, command: str, chat_id, metadata: Optional[dict] = None) -> Optional[R]:
        """Spawn the job, send the "running" message with Kill/Tail buttons,
        register the record, and kick off the watcher.

        ``chat_id`` may be a combined "<chat>:<thread>" token. Returns the
        created `JobRecord` on success, or ``None`` if the spawn failed (a
        failure message is sent to ``chat_id`` in that case).
        """
        try:
            rec = await self._spawn(command, chat_id)
            rec.metadata = metadata
        except Exception as exc:
            log.exception("/%s spawn failed", self.verb)
            await send_message(self.bot, chat_id, f"Failed to spawn: {exc}")
            return None

        sent = await send_message(
            self.bot, chat_id,
            self._status_text(rec, "running"),
            reply_markup=self._keyboard(rec.id, alive=True),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        rec.message_id = sent.message_id
        self._records[rec.id] = rec
        rec.watcher = asyncio.create_task(self._watch(rec))
        log.info("Spawned /%s %s for: %r", self.verb, rec.id, command)
        return rec

    async def _watch(self, rec: R) -> None:
        try:
            status = await self._wait_for_exit(rec)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Watcher for %s %s failed", self.verb, rec.id)
            status = "watcher error"

        rec.final_status = status
        log.info("/%s %s: %s", self.verb, rec.id, status)
        try:
            await self.bot.edit_message_text(
                chat_id=resolve_chat_target(rec.chat_id)[0],
                message_id=rec.message_id,
                text=self._status_text(rec, status),
                reply_markup=self._keyboard(rec.id, alive=False),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            log.exception("Failed to edit /%s message for %s", self.verb, rec.id)

    async def _on_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        action, _, job_id = query.data.partition(":")
        rec = self._records.get(job_id)

        if action == self._kill_action:
            if rec is None or not await self._is_alive(rec):
                await query.answer("already finished")
                return
            await self._kill(rec)
            await query.answer("killed")
            return

        # tail
        if rec is None:
            tail = "(unknown id)"
        else:
            tail = await self._tail(rec)
        target = self.chat_id if self.chat_id is not None else query.message.chat.id
        await send_code_block(self.bot, target, tail or "(no output)")
        await query.answer()

"""`/claude <session_id>` extension: resume a Claude Code session inside a
detached tmux session.

Resolves the original working directory from
``~/.claude/projects/*/<session_id>.jsonl`` via
:func:`toolbox.scripts.get_claude_cwd.get_claude_cwd` and runs
``cd <cwd> && claude --resume <session_id> --remote-control`` inside a
new tmux session. All buttons / lifecycle behaviour are inherited from
:class:`TmuxExtension`.

Usage:

    from telegram.ext import ApplicationBuilder
    from toolbox.teleserver import ClaudeExtension

    app = ApplicationBuilder().token(TOKEN).build()
    ClaudeExtension(app, chat_id=MY_CHAT)
    app.run_polling()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import shlex
import time
from typing import Optional

from telegram import Update
from telegram.ext import CallbackQueryHandler, ContextTypes
from telegram.helpers import escape_markdown

from toolbox.scripts.get_claude_cwd import get_claude_cwd

from .job import JobRecord
from .tmux import TmuxExtension

log = logging.getLogger("main")

TRUST_PROMPT_MARKER = "trust this folder"
TRUST_POLL_INTERVAL = 0.5
TRUST_WAIT_TIMEOUT = 15.0


class ClaudeExtension(TmuxExtension):
    verb = "claude"
    callback_prefix = "c"
    session_prefix = "claude"

    SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{30,40}$")

    def __init__(self, application, button_prefix="claude", verbose=False, **kw,) -> None:
        kw.setdefault("reject_sensitive", False)
        super().__init__(application, **kw)
        self._button_prefix = button_prefix
        self._verbose = verbose
        application.add_handler(CallbackQueryHandler(self._on_claude_button, pattern=f"^{self._button_prefix}:"))


    async def _spawn(self, command: str, chat_id: int) -> JobRecord:
        rec = await super()._spawn(command, chat_id)
        asyncio.create_task(self._accept_trust_loop(rec))
        return rec

    async def _accept_trust_loop(self, rec: JobRecord) -> None:
        deadline = time.monotonic() + TRUST_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            if not await self._alive(rec.id):
                return
            rc, out, _ = await self._tmux("capture-pane", "-p", "-t", f"{rec.id}:")
            if rc == 0 and TRUST_PROMPT_MARKER in out.decode(errors="replace"):
                await self._tmux("send-keys", "-t", f"{rec.id}:", "Down", "Enter")
                log.info("Auto-accepted trust prompt in %s", rec.id)
                return
            await asyncio.sleep(TRUST_POLL_INTERVAL)

    def _parse_input(self, text: str) -> Optional[str]:
        m = re.match(rf"^/{re.escape(self.verb)}(?:@\S+)?\s+(\S+)\s*$", text)
        if not m:
            return None
        sess = m.group(1)
        if not self.SESSION_ID_RE.match(sess):
            return None
        cwd = get_claude_cwd(sess)
        if not cwd:
            return None
        return (
            f"cd {shlex.quote(cwd)} && "
            f"claude --dangerously-skip-permissions --resume {shlex.quote(sess)} --remote-control"
        )

    def _usage(self) -> str:
        return (
            f"Usage: /{self.verb} <session_id>\n"
            f"(session_id is a UUID; cwd resolved via get_claude_cwd)"
        )

    
    async def _on_claude_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        data = (query.data or "").split(":")
        if len(data) != 2 or data[0] != self._button_prefix:
            await query.answer("Invalid button.", show_alert=True)
            return
        session_id = data[1]
        
        command = self._parse_input(f"/claude {session_id}")
        if not command:
            # Malformed session id, or its cwd can't be resolved (folder gone /
            # session expired). Nothing to resume — answer and bail instead of
            # spawning a None command, which crashes in _spawn (exports + None).
            await query.answer(
                "Can't resume: session expired or its folder is gone.",
                show_alert=True,
            )
            return

        await query.answer()
        await self._start_job(command, self.chat_id, metadata={"message_text": query.message.text, "session_id": session_id})

    
    def _status_text(self, rec: JobRecord, status: str) -> str:
        # Everything here renders as MarkdownV2: escape the plain-text status
        # (e.g. "auto-killed after timeout" — the '-' is reserved) and the
        # code-span values (a hyphenated job id / UUID session id is fine inside
        # backticks, but a stray backtick would still break parsing).
        job_id = escape_markdown(rec.id, version=2, entity_type="code")
        status_text = escape_markdown(status, version=2)
        text = f"tmux `{job_id}` — {status_text}"
        session_id = rec.metadata.get("session_id")
        if session_id:
            sid = escape_markdown(str(session_id), version=2, entity_type="code")
            text += f"\nSession: `{sid}`"
        if self._verbose:
            cmd = escape_markdown(rec.cmd, version=2, entity_type="code")
            text += f"\n`{cmd}`"
        return text
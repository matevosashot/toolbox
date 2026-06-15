"""`/tmux` extension: run interactive commands inside a detached tmux session.

Each session is reachable from any terminal via ``tmux attach -t <name>``.
See :class:`toolbox.teleserver.job.JobExtension` for the shared lifecycle.

Usage:

    from telegram.ext import ApplicationBuilder
    from toolbox.teleserver import TmuxExtension

    app = ApplicationBuilder().token(TOKEN).build()
    TmuxExtension(app, chat_id=MY_CHAT)
    app.run_polling()
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import time
from typing import Optional

from telegram.ext import Application
from telegram.helpers import escape_markdown

from .job import JobExtension, JobRecord
from .utils import (
    ANSI_RE,
    TAIL_LINES,
    TWO_DAYS_SECONDS,
    read_tail,
)

log = logging.getLogger("main")

TMUX_POLL_INTERVAL = 10.0


class TmuxExtension(JobExtension[JobRecord]):
    verb = "tmux"
    callback_prefix = "t"
    session_prefix = "tele"

    def __init__(
        self,
        application: Application,
        *,
        session_prefix: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        poll_interval: float = TMUX_POLL_INTERVAL,
        **kw,
    ) -> None:
        if session_prefix is not None:
            self.session_prefix = session_prefix
        # Extra environment variables injected into each spawned tmux session
        # (prepended as `export KEY=VALUE` to the command).
        self.env: dict[str, str] = dict(env) if env else {}
        # The CallbackQueryHandler pattern in JobExtension uses `id_pattern`;
        # tighten it to "<session_prefix>-<digits>-<digits>" so other tmux
        # extensions in the same Application don't claim our callbacks.
        self.id_pattern = rf"{self.session_prefix}-\d+-\d+"
        self.poll_interval = poll_interval
        self._counter: int = 0
        super().__init__(application, **kw)

    def _next_name(self) -> str:
        self._counter += 1
        return f"{self.session_prefix}-{os.getpid()}-{self._counter}"

    @staticmethod
    async def _tmux(*args: str) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out, err

    async def _alive(self, name: str) -> bool:
        rc, _, _ = await self._tmux("has-session", "-t", f"={name}")
        return rc == 0

    async def _spawn(self, command: str, chat_id: int) -> JobRecord:
        os.makedirs(self.log_dir, exist_ok=True)
        name = self._next_name()
        log_path = os.path.join(self.log_dir, f"tmux-{name}.log")
        try:
            open(log_path, "wb").close()
        except OSError:
            log.exception("Failed to create tmux log file %s", log_path)

        # `new-session -e` needs tmux >= 3.2; prepend `export`s instead so the
        # vars are set in the same shell that runs the command (works on 3.0a).
        exports = "".join(
            f"export {key}={shlex.quote(value)}; " for key, value in self.env.items()
        )
        print("exports", exports)
        print("command", command)
        full_command = exports + command

        rc, _, err = await self._tmux("new-session", "-d", "-s", name, full_command)
        if rc != 0:
            raise RuntimeError(
                f"tmux new-session failed: {err.decode(errors='replace').strip()}"
            )

        # Mirror pane output so Tail keeps working after the session ends.
        pipe_cmd = f"cat >> {shlex.quote(log_path)}"
        rc, _, err = await self._tmux("pipe-pane", "-o", "-t", f"{name}:", pipe_cmd)
        if rc != 0:
            log.warning("pipe-pane failed for %s: %s", name, err.decode(errors="replace"))

        return JobRecord(
            id=name,
            cmd=command,
            chat_id=chat_id,
            log_path=log_path,
            started_at=time.time(),
        )

    async def _is_alive(self, rec: JobRecord) -> bool:
        return await self._alive(rec.id)

    async def _wait_for_exit(self, rec: JobRecord) -> str:
        deadline = time.monotonic() + self.auto_kill_seconds
        while True:
            await asyncio.sleep(self.poll_interval)
            if not await self._alive(rec.id):
                return "ended"
            if time.monotonic() >= deadline:
                await self._tmux("kill-session", "-t", f"={rec.id}")
                return "auto-killed after timeout"

    async def _kill(self, rec: JobRecord) -> None:
        await self._tmux("kill-session", "-t", f"={rec.id}")

    async def _tail(self, rec: JobRecord) -> str:
        if await self._alive(rec.id):
            rc, out, _ = await self._tmux(
                "capture-pane", "-p", "-J", "-S", f"-{TAIL_LINES * 4}", "-t", f"{rec.id}:"
            )
            text = out.decode(errors="replace") if rc == 0 else ""
        else:
            text = ANSI_RE.sub("", read_tail(rec.log_path, TAIL_LINES * 4))

        lines = [ln.rstrip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln][-TAIL_LINES:]
        return "\n".join(lines)

    def _status_text(self, rec: JobRecord, status: str) -> str:
        header = escape_markdown(f"tmux {rec.id} — {status}", version=2)
        cmd = escape_markdown(rec.cmd, version=2, entity_type="code")
        return f"*{header}*\n`{cmd}`"

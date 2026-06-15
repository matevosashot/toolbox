"""`/process` extension: spawn long-running detached commands with Kill/Tail
buttons. See :class:`toolbox.teleserver.job.JobExtension` for the shared
lifecycle.

Usage:

    from telegram.ext import ApplicationBuilder
    from toolbox.teleserver import ProcessExtension

    app = ApplicationBuilder().token(TOKEN).build()
    ProcessExtension(app, chat_id=MY_CHAT)
    app.run_polling()
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

from telegram.helpers import escape_markdown

from .job import JobExtension, JobRecord
from .utils import TAIL_LINES, kill_pgid, read_tail


@dataclass
class ProcRecord(JobRecord):
    proc: asyncio.subprocess.Process = None  # type: ignore[assignment]
    pgid: int = 0


class ProcessExtension(JobExtension[ProcRecord]):
    verb = "process"
    callback_prefix = "p"
    id_pattern = r"\d+"

    async def _spawn(self, command: str, chat_id: int) -> ProcRecord:
        os.makedirs(self.log_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="proc.", suffix=".log", dir=self.log_dir
        )
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=tmp_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            try:
                os.close(tmp_fd)
            except OSError:
                pass

        log_path = os.path.join(self.log_dir, f"{proc.pid}.log")
        try:
            os.rename(tmp_path, log_path)
        except OSError:
            log_path = tmp_path

        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid

        return ProcRecord(
            id=str(proc.pid),
            cmd=command,
            chat_id=chat_id,
            log_path=log_path,
            started_at=time.time(),
            proc=proc,
            pgid=pgid,
        )

    async def _is_alive(self, rec: ProcRecord) -> bool:
        return rec.proc.returncode is None

    async def _wait_for_exit(self, rec: ProcRecord) -> str:
        try:
            rc = await asyncio.wait_for(rec.proc.wait(), timeout=self.auto_kill_seconds)
            return f"exited (code {rc})"
        except asyncio.TimeoutError:
            kill_pgid(rec.pgid)
            try:
                await rec.proc.wait()
            except Exception:
                pass
            return "auto-killed after timeout"

    async def _kill(self, rec: ProcRecord) -> None:
        kill_pgid(rec.pgid)

    async def _tail(self, rec: ProcRecord) -> str:
        return read_tail(rec.log_path, TAIL_LINES)

    def _status_text(self, rec: ProcRecord, status: str) -> str:
        header = escape_markdown(f"PID {rec.id} — {status}", version=2)
        cmd = escape_markdown(rec.cmd, version=2, entity_type="code")
        return f"*{header}*\n`{cmd}`"

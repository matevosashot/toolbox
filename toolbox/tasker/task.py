"""Task types executed by a :class:`~toolbox.tasker.worker.Worker`.

A *task* is a script file dropped into ``pending/``. :class:`Task` handles the
lifecycle (acquire -> run -> completed/failed) and the interpreter-selection
rules; :class:`TaskArray` adds the ``[N]``-style repeat-count grammar. See the
:mod:`toolbox.tasker.worker` module docstring for the full design.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

from .constraints import Constraints, parse_script

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .worker import Worker


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Format reused for every timestamp embedded in a file name. Keep it
# lexicographically sortable so `ls` shows tasks in chronological order.
_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


def _now() -> str:
    return datetime.now().strftime(_TIMESTAMP_FMT)


def _hostname() -> str:
    return os.uname()[1]


# --------------------------------------------------------------------------- #
# Task
# --------------------------------------------------------------------------- #

class Task:
    """A single bash script to be executed by a :class:`Worker`.

    The lifecycle is::

        Task(name)  ->  acquire()  ->  run()  ->  completed() / failed()

    Each step renames the underlying file so its location on disk
    reflects its state.
    """

    # Pattern matched against `task_name` to decide which Task subclass to
    # instantiate. The base class accepts anything.
    _PATTERN: Optional[re.Pattern] = None

    def __init__(self, task_name: str, worker: "Worker"):
        self.pending_task: str = task_name
        self.worker: "Worker" = worker
        self.worker_name: str = worker.worker_name

        # Filled in once the corresponding lifecycle step succeeds.
        self.running_task: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.stdout_path: Optional[str] = None
        # Set to True if the worker kills the subprocess because the
        # running/<file> entry was deleted out from under it. Tells
        # Worker.run() to skip the rename to failed/ (the file is gone).
        self.cancelled: bool = False
        # Extra environment variables layered on top of os.environ for the
        # subprocess (e.g. CUDA_VISIBLE_DEVICES from a #WORKER_GPU_DEVICES
        # directive). Populated by the worker before run().
        self.extra_env: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def acquire(self) -> Optional["Task"]:
        """Atomically move the task from ``pending/`` to ``running/``.

        Returns ``self`` on success, ``None`` if another worker won the
        race (or the file disappeared for any other reason).
        """
        src = os.path.join(self.worker.pending_dir, self.pending_task)
        # Acquire timestamp goes at the front so `ls running/` sorts
        # chronologically across heterogeneous task names.
        dest_name = f"{_now()}__{self.worker_name}__{self.pending_task}"
        dest = os.path.join(self.worker.running_dir, dest_name)

        try:
            # os.rename is atomic on the same filesystem and raises
            # FileNotFoundError if `src` was claimed by another worker
            # in the meantime — exactly the semantics we want.
            os.rename(src, dest)
        except FileNotFoundError:
            return None
        except OSError as exc:
            self.worker.logger.warning(
                "Could not acquire %s: %s", self.pending_task, exc
            )
            return None

        self.running_task = dest_name
        return self

    def run(self) -> int:
        """Execute the script and return its exit code."""
        if self.running_task is None:
            raise RuntimeError("Task.run() called before successful acquire().")

        task_path = os.path.join(self.worker.running_dir, self.running_task)
        self.stdout_path = os.path.join(
            self.worker.stdout_dir, f"{self.running_task}.out"
        )

        self.worker.logger.info("Check output at\n%s", self.stdout_path)
        self.exit_code = self._run_script(task_path, self.stdout_path)
        return self.exit_code

    def completed(self) -> None:
        """Move the task into the ``completed/`` directory."""
        self._move_to(self.worker.completed_dir, prefix=f"{_now()}__")

    def failed(self) -> None:
        """Move the task into the ``failed/`` directory.

        The completion timestamp is prepended (so ``ls failed/`` sorts
        chronologically); the exit code is appended for easy triage.
        """
        self._move_to(
            self.worker.failed_dir,
            prefix=f"{_now()}__",
            suffix=f"__{self.exit_code}",
        )

    # ------------------------------------------------------------------ #
    # Constraints                                                         #
    # ------------------------------------------------------------------ #

    def constraints(self) -> Constraints:
        """Parse the ``#WORKER_*`` resource directives from the pending file."""
        path = os.path.join(self.worker.pending_dir, self.pending_task)
        return parse_script(path)

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _move_to(self, target_dir: str, prefix: str = "", suffix: str = "") -> None:
        if self.running_task is None:
            return
        src = os.path.join(self.worker.running_dir, self.running_task)
        dest = os.path.join(target_dir, f"{prefix}{self.running_task}{suffix}")
        os.rename(src, dest)

    # Map file extension to the interpreter used to execute it. Takes
    # precedence over the executable bit and any shebang. Override on
    # subclasses to add languages.
    _EXTENSION_INTERPRETERS = {
        ".py": "python3",
    }

    def _run_script(self, script_path: str, output_path: str) -> int:
        """Run *script_path*, tee-ing combined stdout/stderr to *output_path*.

        ``set -o pipefail`` is critical: without it the exit code of a
        ``cmd | tee`` pipeline is always ``tee``'s (which is 0), so
        every failed task would be misclassified as a success.

        While the subprocess runs, the worker watches *script_path* —
        the entry under ``running/`` — and if it disappears (the user
        ``rm``'d it to cancel the task), the whole process group is
        killed. ``--watch-poll 0`` disables this and falls back to a
        plain blocking wait.
        """
        cmd = self._build_shell_command(script_path, output_path)
        poll = self.worker.watch_poll

        # start_new_session=True puts bash and the entire pipeline
        # (`cmd | tee`) into a new process group, so a single killpg
        # reaps everything.
        proc = subprocess.Popen(
            cmd, shell=True, executable="/bin/bash",
            start_new_session=True,
            env=self._subprocess_env(),
        )

        if poll <= 0:
            proc.wait()
            return proc.returncode

        while True:
            try:
                proc.wait(timeout=poll)
                return proc.returncode
            except subprocess.TimeoutExpired:
                pass
            if not os.path.exists(script_path):
                self.cancelled = True
                self.worker.logger.warning(
                    "Running file %s was deleted; killing task.",
                    self.running_task,
                )
                self._terminate(proc)
                return proc.returncode

    def _subprocess_env(self) -> Optional[Dict[str, str]]:
        """Return the environment for the task subprocess.

        ``None`` (inherit the worker's environment unchanged) when there are
        no overrides; otherwise a copy of ``os.environ`` with
        :attr:`extra_env` layered on top.
        """
        if not self.extra_env:
            return None
        env = dict(os.environ)
        env.update(self.extra_env)
        return env

    def _terminate(self, proc: subprocess.Popen, grace: float = 5.0) -> None:
        """SIGTERM the subprocess group, escalate to SIGKILL after *grace*.

        Killing the group (rather than just ``proc.pid``) ensures the
        whole ``bash`` pipeline — interpreter, the script, and ``tee``
        — goes down together.
        """
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            self.worker.logger.warning(
                "Task %s did not exit %.1fs after SIGTERM; sending SIGKILL.",
                self.running_task, grace,
            )
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait()

    def _runner_command(self, script_path: str) -> str:
        """Return the shell snippet that invokes *script_path*.

        Resolution order:

        1. If the original task name has an extension registered in
           :attr:`_EXTENSION_INTERPRETERS` (e.g. ``.py``) → use that
           interpreter, regardless of the executable bit or shebang.
        2. Else if the file is executable → exec it directly so its
           shebang chooses the interpreter.
        3. Else → run with ``bash`` (lets users drop plain shell
           snippets into ``pending/`` without having to ``chmod +x``).
        """
        ext = os.path.splitext(self.pending_task)[1].lower()
        interpreter = self._EXTENSION_INTERPRETERS.get(ext)
        if interpreter is not None:
            return f'{interpreter} "{script_path}"'
        if os.access(script_path, os.X_OK):
            return f'"{script_path}"'
        return f'bash "{script_path}"'

    def _script_args(self) -> List[str]:
        """Extra positional args appended after the script path.

        Default: none. Override in subclasses (see :class:`TaskArray`,
        which appends ``num`` as ``$1``).
        """
        return []

    def _build_shell_command(self, script_path: str, output_path: str) -> str:
        cmd = self._runner_command(script_path)
        for arg in self._script_args():
            cmd += f' "{arg}"'
        return f'set -o pipefail; {cmd} 2>&1 | tee "{output_path}"'

    # ------------------------------------------------------------------ #
    # Classification                                                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def matches(cls, task_name: str) -> bool:
        """Return True if *task_name* should be handled by this class."""
        return cls._PATTERN is None or cls._PATTERN.match(task_name) is not None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.pending_task!r})"


# --------------------------------------------------------------------------- #
# TaskArray
# --------------------------------------------------------------------------- #

class TaskArray(Task):
    """A task whose file name encodes a remaining iteration count.

    Examples of valid names::

        [10]train          # run 10 times, propagate
        ![5]train          # priority, run 5 times, propagate
        *[3]train          # run once at N=3, do NOT propagate
        !*[3]train         # priority + non-propagating

    On acquisition, if ``num > 0`` and the ``*`` flag is absent, a copy
    named ``<flags>[num-1]<name>`` is dropped back into ``pending/``.
    The current ``num`` is passed to the script as ``$1``.
    """

    # Single source of truth for the file-name grammar.
    _PATTERN = re.compile(r"^(?P<flags>[!*]*)\[(?P<num>\d+)\](?P<name>.*)$")

    def __init__(self, task_name: str, worker: "Worker"):
        super().__init__(task_name, worker=worker)
        decomposed = self._decompose(task_name)
        if decomposed is None:
            raise ValueError(f"{task_name!r} is not a valid TaskArray name.")
        self.flags, self.num, self.name = decomposed

    # ------------------------------------------------------------------ #

    def acquire(self) -> Optional["TaskArray"]:
        acquired = super().acquire()
        if acquired is None:
            return None

        # Only spawn a follow-up if there are iterations left and the
        # array has not been frozen with the `*` flag.
        if self.num > 0 and "*" not in self.flags:
            self._spawn_next()

        return self

    def _spawn_next(self) -> None:
        """Create the ``[num-1]name`` follow-up file in pending/."""
        src = os.path.join(self.worker.running_dir, self.running_task)
        next_name = f"{self.flags}[{self.num - 1}]{self.name}"
        dest = os.path.join(self.worker.pending_dir, next_name)
        # `cp` instead of `os.rename` because the original is now in
        # `running/` and must stay there for execution.
        subprocess.run(["cp", src, dest], check=False)

    def _script_args(self) -> List[str]:
        # Pass the current iteration count as $1 to the script.
        return [str(self.num)]

    # ------------------------------------------------------------------ #

    @classmethod
    def _decompose(cls, task_name: str):
        match = cls._PATTERN.match(task_name)
        if match is None:
            return None
        return match.group("flags"), int(match.group("num")), match.group("name")


# All concrete Task classes, in priority order: the first one whose
# `matches()` returns True is used to wrap the file name.
_TASK_TYPES = (TaskArray, Task)

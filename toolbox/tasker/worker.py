"""File-system based task queue and worker.

Overview
--------
A *task* is simply a bash script file dropped into a "pending" directory.
One or more *workers* (one per machine, typically) poll that directory and
race to claim a script via an atomic rename. The winning worker executes
the script, captures its stdout/stderr to a log file, and then moves the
script into either ``completed/`` or ``failed/`` depending on the exit
code.

Because acquisition is a single ``os.rename`` call within the same
filesystem, claiming is race-free without any external lock service:
exactly one worker succeeds, the others get :class:`FileNotFoundError`
and move on. This makes it easy to run a shared task pool over an NFS /
SMB / SSHFS mount with no database.

On-disk layout
--------------
::

    <task_base_path>/
        pending/        # tasks waiting to run (filename == script)
        running/        # tasks currently executing
        completed/      # tasks finished with exit code 0
        failed/         # tasks finished with non-zero exit code
        logs/           # per-worker log files (<worker_name>.log)
        stdout/         # stdout/stderr capture of every task (override
                        #   with --stdout-dir / Worker(stdout_dir=...))
        archive/        # reserved for manual archival

How tasks are executed
----------------------
The interpreter used for a task file is chosen by:

1. **Extension** – ``.py`` files are run with ``python3``. (Add more
   languages by extending :attr:`Task._EXTENSION_INTERPRETERS`.)
2. **Executable bit** – if the file has ``+x``, it's exec'd directly,
   so its shebang line chooses the interpreter.
3. **Default** – any other file is fed to ``bash``. This lets you drop
   plain shell snippets without ``chmod +x``.

In all cases, combined stdout/stderr is captured to the configured
``stdout_dir`` via ``tee``, and ``set -o pipefail`` is set so the task
exit code reflects the script (not ``tee``).

File-name conventions
---------------------
The base file name is the script name. Optional prefixes/suffixes change
how the worker treats it:

* ``!task``       – *priority* task. Workers prefer these over normal
                    tasks when both are present.
* ``[N]task``     – *task array*. The script is run, then a copy named
                    ``[N-1]task`` is dropped back into ``pending/`` so
                    that the same script ultimately runs ``N`` times.
                    The current value of ``N`` is passed to the script
                    as ``$1``. Stops automatically at ``[0]task``.
* ``*[N]task``    – task array that does *not* re-spawn itself. Useful
                    to stop a running array without deleting the file.
* ``![N]task``    – priority task array (flags can be combined).

When the task is moved to ``running/`` it is renamed to
``<original>__<worker>__<YYYYMMDD-HHMMSS>``. After completion an extra
``__<YYYYMMDD-HHMMSS>`` (and ``__<exit_code>`` on failure) is appended
so the history is self-documenting from ``ls``.

Usage
-----
Single iteration (process one task and exit)::

    python -m toolbox.tasker.worker -p /path/to/task_base_path

Long-running daemon (poll forever)::

    python -m toolbox.tasker.worker -p /path/to/task_base_path --loop

Without ``-p``, the path is read from the ``TASK_BASE_PATH`` env var.

Submitting a task is just dropping a file into ``pending/``::

    cat > /path/to/task_base_path/pending/train_model_v3 <<'EOF'
    #!/usr/bin/env bash
    set -euo pipefail
    python train.py --epochs 10
    EOF

To run that script three times (with $1 = 3, then 2, then 1)::

    mv .../pending/train_model_v3 .../pending/'[3]train_model_v3'

When ``--loop`` is used, the worker is self-recovering: any exception
raised by a single iteration is caught, logged with its traceback, and
followed by a ``--restart-sleep`` pause before the next iteration. Only
``KeyboardInterrupt`` aborts the daemon. This makes the previous
``worker.sh`` shell-level restart wrapper unnecessary.
"""

from __future__ import annotations

import argparse
import logging
import os
import random as _random  # aliased to avoid clashing with the `randomize` arg
import re
import signal
import subprocess
import time
import traceback
from datetime import datetime
from typing import List, Optional

from toolbox.logging_utils import setup_loggers


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


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

class Worker:
    """Polls a task directory and runs whatever it finds.

    Args:
        task_base_path: Root directory containing ``pending/``,
            ``running/``, etc. Created if missing.
        worker_name: Identifier embedded into running/completed file
            names. Defaults to the host's name (``uname -n``).
        randomize: Pick the next pending task at random instead of
            taking the first listed one. Strongly recommended when
            multiple workers share a directory — it dramatically cuts
            down on acquisition collisions.
        idle_sleep: Seconds to sleep when the queue is empty.
        failure_sleep: Seconds to sleep after a failed task.
        restart_sleep: Seconds to sleep after an unhandled exception in
            :meth:`loop` before resuming. Mirrors what the old
            ``worker.sh`` wrapper used to do at the process level.
        loop_tick: Minimum delay between polling iterations.
        watch_poll: Seconds between checks for the running-file having
            been deleted. When the entry under ``running/`` disappears,
            the worker treats it as a manual cancellation and kills the
            running subprocess group. ``0`` disables the watch and
            falls back to a plain blocking ``wait()``.
        stdout_dir: Where to write per-task stdout/stderr capture files.
            Defaults to ``<task_base_path>/stdout/`` so each task tree
            keeps its outputs alongside ``completed/`` / ``failed/`` and
            different projects don't clobber each other. Pass an
            explicit path (``~`` is expanded) to override.
        logger: Logger to use. If ``None``, uses
            ``main.tasker.<worker_name>``. Following the standard-library
            convention, the worker class itself does not attach any
            handlers — configure them at the application boundary (e.g.
            via :func:`toolbox.setup_loggers`). The script entry point
            in :func:`main` does this for you.
    """

    def __init__(
        self,
        task_base_path: str,
        worker_name: Optional[str] = None,
        randomize: bool = True,
        idle_sleep: float = 10.0,
        failure_sleep: float = 2.0,
        restart_sleep: float = 5.0,
        loop_tick: float = 0.1,
        watch_poll: float = 5.0,
        stdout_dir: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.task_base_path = task_base_path
        self.worker_name = worker_name or _hostname()
        self.randomize = randomize
        self.idle_sleep = idle_sleep
        self.failure_sleep = failure_sleep
        self.restart_sleep = restart_sleep
        self.loop_tick = loop_tick
        self.watch_poll = watch_poll
        # stdout_dir is the only directory not derived from
        # task_base_path, so it's a plain attribute rather than a
        # @property. Default keeps task artifacts colocated.
        self.stdout_dir = (
            os.path.expanduser(stdout_dir)
            if stdout_dir is not None
            else os.path.join(task_base_path, "stdout")
        )

        self._init_dirs()

        self.logger = logger or logging.getLogger(f"main.tasker.{self.worker_name}")
        self.logger.info("Worker %s initialised at %s.",
                         self.worker_name, self.task_base_path)

    # ------------------------------------------------------------------ #
    # Directory layout                                                    #
    # ------------------------------------------------------------------ #

    @property
    def pending_dir(self) -> str:
        return os.path.join(self.task_base_path, "pending")

    @property
    def running_dir(self) -> str:
        return os.path.join(self.task_base_path, "running")

    @property
    def completed_dir(self) -> str:
        return os.path.join(self.task_base_path, "completed")

    @property
    def failed_dir(self) -> str:
        return os.path.join(self.task_base_path, "failed")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.task_base_path, "logs")

    @property
    def archive_dir(self) -> str:
        return os.path.join(self.task_base_path, "archive")

    def _init_dirs(self) -> None:
        for d in (
            self.pending_dir,
            self.running_dir,
            self.stdout_dir,
            self.failed_dir,
            self.completed_dir,
            self.logs_dir,
            self.archive_dir,
        ):
            os.makedirs(d, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Polling                                                             #
    # ------------------------------------------------------------------ #

    def _list_pending(self) -> List[str]:
        try:
            entries = os.listdir(self.pending_dir)
        except FileNotFoundError:
            os.makedirs(self.pending_dir, exist_ok=True)
            return []
        # Skip dotfiles and editor leftovers like `.foo.swp` / `foo~`.
        return [e for e in entries if not e.startswith(".") and not e.endswith("~")]

    def get_pending_task(self) -> Optional[Task]:
        """Pick a single pending task and wrap it in the right Task class.

        Priority tasks (``!``-prefixed) are preferred over the rest; the
        chosen subset is then sampled randomly or in directory order.
        """
        tasks = self._list_pending()
        if not tasks:
            return None

        priority = [t for t in tasks if t.startswith("!")]
        self.logger.info(
            "%d pending task%s (%d priority).",
            len(tasks), "" if len(tasks) == 1 else "s", len(priority),
        )
        candidates = priority or tasks

        name = _random.choice(candidates) if self.randomize else candidates[0]

        for task_cls in _TASK_TYPES:
            if task_cls.matches(name):
                return task_cls(name, worker=self)
        # Should never happen because Task.matches always returns True,
        # but guard anyway.
        return Task(name, worker=self)

    # ------------------------------------------------------------------ #
    # Main loop                                                           #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Process at most one task. Safe to call in a loop."""
        task = self.get_pending_task()
        if task is None:
            self.logger.info("No pending tasks. Sleeping for %.1fs...",
                             self.idle_sleep)
            time.sleep(self.idle_sleep)
            return

        original_name = task.pending_task  # keep for log if acquire fails
        if task.acquire() is None:
            self.logger.info(
                "Failed to acquire task %s. It may have been taken by "
                "another worker.", original_name,
            )
            return

        self.logger.info("Acquired task %s. Processing...", task)
        exit_code = task.run()

        if getattr(task, "cancelled", False):
            # The running/<file> entry was removed mid-execution and the
            # subprocess was killed. The file is gone, so don't try to
            # rename it — just log and move on. The .out capture is
            # left in place so the user can inspect partial output.
            self.logger.warning(
                "Task %s cancelled (running file removed). "
                "Stdout left at\n%s",
                task, task.stdout_path,
            )
            time.sleep(self.failure_sleep)
            return

        if exit_code == 0:
            self.logger.info("Task %s completed successfully.", task)
            task.completed()
        else:
            self.logger.error(
                "Task %s failed with exit code %d. Check stdout at\n\n%s",
                task, exit_code, task.stdout_path,
            )
            task.failed()
            time.sleep(self.failure_sleep)

    def loop(self) -> None:
        """Poll forever, handling one task per iteration.

        Unhandled exceptions in a single iteration are logged and the
        loop sleeps for :attr:`restart_sleep` seconds before continuing,
        so transient errors (e.g. a flaky NFS mount) don't kill the
        daemon. ``KeyboardInterrupt`` is the only way out.
        """
        while True:
            try:
                time.sleep(self.loop_tick)
                self.run()
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.exception(
                    "Unhandled error in worker loop; sleeping %.1fs "
                    "before retrying.", self.restart_sleep,
                )
                time.sleep(self.restart_sleep)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="File-system based task worker. See module docstring "
                    "for the on-disk layout and file-name conventions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--task-base-path", "-p",
        type=str,
        default=os.getenv("TASK_BASE_PATH"),
        help="Root task directory. Defaults to the TASK_BASE_PATH env var.",
    )
    parser.add_argument(
        "--worker-name", "-n",
        type=str,
        default=None,
        help="Worker identifier (default: hostname).",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Poll forever instead of processing a single task and exiting.",
    )
    parser.add_argument(
        "--no-random", action="store_true",
        help="Pick the first listed pending task instead of a random one. "
             "Mostly useful when running a single worker.",
    )
    parser.add_argument(
        "--idle-sleep", type=float, default=10.0,
        help="Seconds to sleep when the queue is empty (default: 10).",
    )
    parser.add_argument(
        "--failure-sleep", type=float, default=2.0,
        help="Seconds to sleep after a failed task (default: 2).",
    )
    parser.add_argument(
        "--restart-sleep", type=float, default=5.0,
        help="Seconds to sleep after an unhandled exception in --loop "
             "mode before retrying (default: 5).",
    )
    parser.add_argument(
        "--watch-poll", type=float, default=5.0,
        help="Seconds between checks for the running-task file having "
             "been deleted. If it disappears, the worker kills the "
             "subprocess group. 0 disables (default: 5).",
    )
    parser.add_argument(
        "--stdout-dir", type=str, default=None,
        help="Directory for per-task stdout/stderr capture files. "
             "Defaults to <task_base_path>/stdout/. '~' is expanded.",
    )
    parser.add_argument(
        "--log-path", type=str, default=None,
        help="Worker log file path (or directory). Defaults to "
             "<task_base_path>/logs/<worker_name>.log. If a directory "
             "is given, setup_loggers auto-names the file. '~' is "
             "expanded.",
    )
    parser.add_argument(
        "--telegram", action="store_true",
        help="Forward error-level log records to Telegram (requires "
             "TELEGRAM_BOT_TOKEN to be configured).",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.task_base_path is None:
        parser.error(
            "Task base path is not set. Set the TASK_BASE_PATH environment "
            "variable or pass --task-base-path/-p."
        )

    # Configure logging at the application boundary, not inside Worker.
    # Default log file lives at <task_base_path>/logs/<worker_name>.log;
    # --log-path overrides that and may be either a .log file or a
    # directory (setup_loggers handles both cases).
    worker_name = args.worker_name or _hostname()
    log_path = (
        os.path.expanduser(args.log_path)
        if args.log_path is not None
        else os.path.join(args.task_base_path, "logs", f"{worker_name}.log")
    )
    setup_loggers(
        base_path=log_path,
        telegram=args.telegram,
        train_logger=False,
        stdout=True,
    )

    worker = Worker(
        task_base_path=args.task_base_path,
        worker_name=worker_name,
        randomize=not args.no_random,
        idle_sleep=args.idle_sleep,
        failure_sleep=args.failure_sleep,
        restart_sleep=args.restart_sleep,
        watch_poll=args.watch_poll,
        stdout_dir=args.stdout_dir,
    )

    try:
        if args.loop:
            worker.loop()
        else:
            worker.run()
    except KeyboardInterrupt:
        worker.logger.info(
            "Worker %s interrupted by user. Exiting...", worker.worker_name
        )
        raise SystemExit(0)
    except Exception as exc:
        worker.logger.error(
            "Worker %s encountered an error: %s", worker.worker_name, exc
        )
        worker.logger.error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()

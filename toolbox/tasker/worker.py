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

Resource constraints
--------------------
A job script may advertise the resources it needs via ``#WORKER_*`` header
comment lines. Before claiming a task the worker parses these directives and
checks the machine's *current* resources; if any constraint is unmet the task
is left untouched in ``pending/`` and re-checked on the next poll (it is never
auto-failed). Directives are scanned from the top of the file and parsing
stops at the first line of real code, so keep them in the header::

    #!/usr/bin/env bash
    #WORKER_GPU_MEM 20GB      # min free memory required on each target GPU
    #WORKER_GPU_LOAD 80%      # each target GPU's utilisation must be < 80%
    #WORKER_MEM 100GB         # min available system RAM
    #WORKER_GPU_DEVICES 0,1   # GPU indices the task uses (also exported as
                              #   CUDA_VISIBLE_DEVICES to the task)
    python train.py

Sizes accept ``KB/MB/GB/TB`` and ``KiB/MiB/GiB/TiB`` (binary, 1024-based).
The set of GPUs each constraint is checked against is resolved as:
``#WORKER_GPU_DEVICES`` → ``CUDA_VISIBLE_DEVICES`` in the worker's environment
→ all GPUs. Every GPU in that set must *individually* satisfy the GPU
constraints. See :mod:`toolbox.tasker.constraints` for details.

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
import time
import traceback
from typing import List, Optional, Tuple

from toolbox.logging_utils import setup_loggers

# Task / TaskArray now live in task.py; re-exported here for backward
# compatibility (`from toolbox.tasker.worker import Task` still works).
from .resources import GpuInfo, available_ram, query_gpus
from .task import Task, TaskArray, _hostname, _now, _TASK_TYPES  # noqa: F401

__all__ = ["Task", "TaskArray", "Worker", "main"]


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

    def _wrap(self, name: str) -> Task:
        """Wrap a pending file name in the most specific matching Task class."""
        for task_cls in _TASK_TYPES:
            if task_cls.matches(name):
                return task_cls(name, worker=self)
        # Should never happen because Task.matches always returns True,
        # but guard anyway.
        return Task(name, worker=self)

    def _ordered_candidates(self, tasks: List[str]) -> List[str]:
        """Return pending names ordered by preference.

        Priority (``!``-prefixed) tasks come first so they are tried before
        the rest, but non-priority tasks remain as a fallback (e.g. when every
        priority task is blocked by resource constraints). Within each group
        the order is randomised unless :attr:`randomize` is False.
        """
        priority = [t for t in tasks if t.startswith("!")]
        others = [t for t in tasks if not t.startswith("!")]
        if self.randomize:
            _random.shuffle(priority)
            _random.shuffle(others)
        return priority + others

    def get_pending_task(self) -> Optional[Task]:
        """Pick a runnable pending task, or ``None`` if none can run now.

        Priority tasks (``!``-prefixed) are preferred. Each candidate's
        ``#WORKER_*`` resource constraints are checked against the machine's
        current state; tasks whose constraints are unmet are skipped (left in
        ``pending/``) and the next candidate is tried. The live resource
        snapshot (GPUs + RAM) is probed at most once per call, and only when a
        candidate actually declares a constraint, so the unconstrained fast
        path stays free of ``nvidia-smi``/``psutil`` calls.
        """
        tasks = self._list_pending()
        if not tasks:
            return None

        priority = [t for t in tasks if t.startswith("!")]
        self.logger.info(
            "%d pending task%s (%d priority).",
            len(tasks), "" if len(tasks) == 1 else "s", len(priority),
        )

        probe: Optional[Tuple[List[GpuInfo], Optional[int]]] = None
        blocked: List[Tuple[str, List[str]]] = []

        for name in self._ordered_candidates(tasks):
            task = self._wrap(name)
            constraints = task.constraints()

            if constraints.is_empty():
                return task

            if probe is None:
                # Lazily probe once and reuse for every remaining candidate.
                probe = (query_gpus(), available_ram())
            gpus, ram = probe

            ok, reasons = constraints.check(gpus, ram)
            if not ok:
                blocked.append((name, reasons))
                continue

            cvd = constraints.cuda_visible_devices()
            if cvd is not None:
                task.extra_env["CUDA_VISIBLE_DEVICES"] = cvd
            return task

        if blocked:
            name, reasons = blocked[0]
            self.logger.info(
                "%d task%s blocked by resource constraints; e.g. %s: %s",
                len(blocked), "" if len(blocked) == 1 else "s",
                name, "; ".join(reasons),
            )
        return None

    # ------------------------------------------------------------------ #
    # Main loop                                                           #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Process at most one task. Safe to call in a loop."""
        task = self.get_pending_task()
        if task is None:
            self.logger.info("No runnable tasks. Sleeping for %.1fs...",
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
    parser.add_argument(
        "--print-manual", action="store_true",
        help="Print the manual for the tasker worker.",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.print_manual:
        print(__doc__)
        return

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

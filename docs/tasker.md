# Tasker — file-system task queue

A minimal distributed task queue where each *task* is just a bash script
file dropped into a directory. One or more *workers* poll the directory
and race to claim scripts via an atomic `os.rename`. The winner runs the
script, captures its output, and moves it into `completed/` or `failed/`
based on the exit code.

There is no broker, no database, and no network protocol — the only
shared state is the filesystem. This makes it a good fit for shared
storage (NFS / SMB / SSHFS) where many machines need to drain a common
queue of "jobs to run".

## Table of contents

- [When to use it](#when-to-use-it)
- [Quick start](#quick-start)
- [On-disk layout](#on-disk-layout)
- [File-name grammar](#file-name-grammar)
  - [Priority tasks (`!`)](#priority-tasks-)
  - [Task arrays (`[N]`)](#task-arrays-n)
  - [Non-propagating arrays (`*`)](#non-propagating-arrays-)
  - [Combining flags](#combining-flags)
- [CLI reference](#cli-reference)
- [Programmatic API](#programmatic-api)
  - [`Worker`](#worker)
  - [`Task` and `TaskArray`](#task-and-taskarray)
  - [Custom logging](#custom-logging)
- [Concurrency model](#concurrency-model)
- [Logging](#logging)
- [Crash recovery](#crash-recovery)
- [FAQ / gotchas](#faq--gotchas)
- [Worked example](#worked-example)

## When to use it

Reach for the tasker when **all** of these are true:

- The unit of work is naturally a shell script (model training run,
  data-conversion command, batch download…).
- Workers can share a directory (local FS, NFS, SMB, SSHFS).
- You want to submit jobs from anywhere by just dropping a file.
- You can tolerate at-most-once execution semantics — once a worker
  claims a task it is the only one running it; if it crashes mid-run,
  the file is left in `running/` and won't be retried automatically.

If you need fan-out fan-in, retries, dependencies, priorities beyond a
single boolean, or sub-second scheduling, use a real queue (Celery,
RQ, Temporal, etc.).

## Quick start

```bash
# 1. Pick a directory that all workers can see.
export TASK_BASE_PATH=/shared/jobs

# 2. Submit a task by dropping a script.
cat > "$TASK_BASE_PATH/pending/train_v1" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
python train.py --epochs 10
EOF

# 3. Run a worker (one or many, on any machine).
toolbox worker --loop          # --loop = poll forever
```

The worker logs its progress to `<TASK_BASE_PATH>/logs/<hostname>.log`
and writes per-task stdout/stderr to `~/worker_stdout/<task>.out`.

## On-disk layout

```
<task_base_path>/
├── pending/        # tasks waiting to run (filename = bash script)
├── running/        # tasks currently executing
├── completed/      # tasks that exited 0
├── failed/         # tasks that exited non-zero
├── logs/           # per-worker .log files
└── archive/        # reserved for manual archival

~/worker_stdout/    # stdout/stderr capture of every task
```

When a worker claims a task it renames the file to encode who picked
it up and when. After completion an additional timestamp suffix
(and the exit code on failure) is appended:

```
pending/    train_v1
running/    train_v1__hostA__20260507-172000
completed/  train_v1__hostA__20260507-172000__20260507-172403
failed/     train_v1__hostA__20260507-172000__20260507-172403__7
```

Sorting `ls completed/` is therefore a chronological history.

## File-name grammar

The file name's prefix encodes scheduling hints. Everything after the
optional flags and bracketed counter is the script's "real" name and
has no special meaning to the worker.

```
[!*]*  [N]  <name>
^^^^^  ^^^  ^^^^^^
flags  num  free-form name
```

### Priority tasks (`!`)

A leading `!` makes the worker prefer that file over any non-priority
task in the same `pending/` directory. Useful for jumping the queue:

```bash
mv pending/big_export "pending/!big_export"
```

If multiple priority tasks exist, the same random/FIFO logic applies
within that subset.

### Task arrays (`[N]`)

A bracketed integer prefix turns the file into an "array" that runs
`N+1` times. When the worker claims `[N]name`, before executing it it
copies the script back into `pending/` as `[N-1]name`, so the file
chain naturally drains down to `[0]name` and then stops. The current
value of `N` is passed to the script as `$1`.

```bash
echo '#!/usr/bin/env bash
echo "iteration $1"' > "pending/[3]hello"

# Resulting executions:
#   $1 = 3
#   $1 = 2
#   $1 = 1
#   $1 = 0
```

Different workers can pick up successive iterations in parallel — the
array spawns one new pending file per claim, not one per machine.

### Non-propagating arrays (`*`)

The `*` flag suppresses the auto-spawn step. It's how you "stop" a
running array without deleting files in flight:

```bash
# Make sure no further iterations are queued, but let
# whatever is currently in pending/ finish.
mv "pending/[5]hello" "pending/*[5]hello"
```

Any worker that subsequently picks up `*[5]hello` will run it once
and *not* drop a `[4]hello` follow-up.

### Combining flags

Flags compose freely:

| Name           | Priority? | Spawns next? | $1 |
| -------------- | --------- | ------------ | -- |
| `train`        | no        | n/a (plain)  | -  |
| `!train`       | yes       | n/a          | -  |
| `[5]train`     | no        | yes          | 5  |
| `![5]train`    | yes       | yes          | 5  |
| `*[5]train`    | no        | no           | 5  |
| `!*[5]train`   | yes       | no           | 5  |

Order of the flag characters does not matter (`!*` and `*!` are
equivalent).

## CLI reference

```
toolbox worker [--task-base-path PATH] [--worker-name NAME]
               [--loop] [--no-random]
               [--idle-sleep N] [--failure-sleep N] [--restart-sleep N]
               [--telegram]
```

| Flag               | Default            | Description                                                                                                |
| ------------------ | ------------------ | ---------------------------------------------------------------------------------------------------------- |
| `--task-base-path`, `-p` | `$TASK_BASE_PATH` | Root directory containing `pending/`, `running/`, … Created if missing.                                    |
| `--worker-name`, `-n` | hostname        | Identifier embedded into running/completed file names.                                                     |
| `--loop`           | off                | Poll forever instead of processing a single task and exiting.                                              |
| `--no-random`      | off                | Pick the first listed pending task instead of a random one. Useful when running a single worker.           |
| `--idle-sleep`     | `10`               | Seconds to sleep when the queue is empty.                                                                  |
| `--failure-sleep`  | `2`                | Seconds to sleep after a failed task.                                                                      |
| `--restart-sleep`  | `5`                | Seconds to sleep after an unhandled exception in `--loop` mode before retrying. See [crash recovery](#crash-recovery). |
| `--telegram`       | off                | Forward `ERROR`-level log records to Telegram (requires `TELEGRAM_BOT_TOKEN`).                             |

Equivalent invocations:

```bash
toolbox worker -p /shared/jobs --loop
python -m toolbox.tasker.worker -p /shared/jobs --loop
```

## Programmatic API

### `Worker`

```python
from toolbox.tasker import Worker

w = Worker(
    task_base_path="/shared/jobs",
    worker_name="ml-rig-3",   # optional, defaults to hostname
    randomize=True,           # random pick within pending/
    idle_sleep=10.0,          # queue-empty backoff
    failure_sleep=2.0,        # backoff after a failed task
    restart_sleep=5.0,        # backoff after an uncaught exception
)

w.run()    # process exactly one task and return
w.loop()   # poll forever; resilient to per-iteration exceptions
```

`Worker` does **not** install any logging handlers itself — see
[Custom logging](#custom-logging).

### `Task` and `TaskArray`

These are the lifecycle objects the worker creates internally; you
rarely construct them by hand. The lifecycle is:

```
Task(name)  →  acquire()  →  run()  →  completed() / failed()
```

Each step renames the underlying file so the on-disk state always
reflects the in-memory state. `TaskArray` extends `Task` to handle the
`[N]` re-spawn behaviour and pass `N` as `$1` to the script.

### Adding a new task type

`TaskArray` is a worked example of subclassing. To add your own
scheduling pattern, subclass `Task`, set a `_PATTERN` class attribute
(a compiled regex), and prepend the new class to `_TASK_TYPES` at the
bottom of `worker.py`. The first class whose `matches(name)` returns
True wins. The base `Task` matches everything (its `_PATTERN` is
`None`) and is therefore always the fallback.

### Custom logging

The worker uses a logger named `main.tasker.<worker_name>` and leaves
all handler configuration to the application. The script entry point
(`toolbox worker`) configures handlers via
[`setup_loggers`](../README.md#setup_loggers); when used as a library
you do this yourself, e.g.:

```python
import logging
from toolbox import setup_loggers
from toolbox.tasker import Worker

setup_loggers(
    base_path="/shared/jobs/logs/my_worker.log",
    telegram=False,
    train_logger=False,
    stdout=True,
)

w = Worker(task_base_path="/shared/jobs", worker_name="my_worker")
w.loop()
```

You can also inject any logger directly:

```python
w = Worker(
    task_base_path="/shared/jobs",
    logger=logging.getLogger("my.app.tasker"),
)
```

## Concurrency model

Acquisition is a single `os.rename(pending/foo, running/foo__host__ts)`:

- On the **same filesystem**, POSIX guarantees `rename` is atomic.
  Exactly one of N racing workers succeeds; the others get
  `FileNotFoundError` and silently move on.
- On NFSv3 shared mounts the same guarantee holds (server-side rename).
  On SMB/SSHFS it is generally safe but vendor-dependent — check your
  mount's docs if you intend to run more than a handful of workers.

There is **no central lock**, **no leader election**, and **no
heartbeat**. A worker that crashes mid-execution leaves its file in
`running/`; nothing detects this automatically. If you need automatic
re-queue, periodically `mv running/* pending/` from a janitor cron
(make sure no worker is actually running them).

The acquisition log line `"N pending tasks (M priority)"` is printed
*before* claiming, so two workers will often disagree on `N` — that's
fine and expected.

## Logging

By default `toolbox worker`:

- Writes a `.log` per worker to `<task_base_path>/logs/<worker_name>.log`
- Mirrors all records to stdout
- Skips the `main.train` Telegram handler (this is a worker, not a
  training process)
- Skips Telegram unless `--telegram` is passed

Per-task stdout and stderr are merged and tee'd to
`~/worker_stdout/<running_task_name>.out`. The path is logged at
`INFO` so it's easy to copy from a terminal:

```
INFO  Check output at
/home/me/worker_stdout/train_v1__hostA__20260507-172000.out
```

The `pipefail` shell option is set before running the script so the
exit code reflects the script itself, not `tee`.

## Crash recovery

When run with `--loop`, the worker is **self-recovering**: any
exception raised by a single iteration is caught, logged with its
traceback, followed by `--restart-sleep` seconds of pause, and then
the loop continues. Only `KeyboardInterrupt` aborts.

This replaces the older `worker.sh` shell wrapper that used to relaunch
the Python process on crash. If you still want a process-level
supervisor (e.g. systemd, supervisord), `--loop` plays well with it —
exit code 0 means clean shutdown via `KeyboardInterrupt`.

## FAQ / gotchas

**Q. Two workers picked the same file. Is that possible?**

No. The atomic rename guarantees exactly one wins. The losers see
`FileNotFoundError` from `os.rename`, log a single `"Failed to acquire
…"` info message, and continue.

**Q. My task is running but the worker reports it failed with exit code 1
even though the script looks fine.**

The shell pipeline runs with `set -o pipefail`, so any command in the
pipeline (including the wrapped `bash "$script"`) failing will surface.
If your script intentionally uses `false ||` patterns inside its top
level, prefer wrapping them in functions or `if`-blocks rather than
relying on the global exit being 0.

**Q. The same task is in `pending/` and `running/`. What happened?**

You probably copied a file rather than moved it, or the pre-existing
acquired-then-not-renamed state from an old crash is lingering. Move
the stale `running/` entry into `archive/` (or back into `pending/` if
you want it retried) and continue.

**Q. Can a script schedule more tasks?**

Yes — the script can `cp` or `mv` files into `pending/` like any other
process. This is how you build pipelines: each stage writes the next
stage's task file when it finishes successfully.

**Q. How do I cancel a queued task?**

Just `rm pending/<task>`. The worker will only see whatever is there
at poll time. To cancel a running task, send a signal to the bash
subprocess; the worker will route it to `failed/` based on the exit
code.

**Q. How do I stop a long-running task array?**

Either delete the next pending iteration (`rm pending/[N]name`), or
freeze it in place by adding a `*` flag (`mv pending/[N]name
'pending/*[N]name'`). The `*` variant lets the currently-queued
iteration finish but skips the auto-respawn.

## Worked example

A complete runnable example lives in [`examples/worker/`](../examples/worker/README.md).
It includes one task of each kind (plain, priority, task array,
failing) and a `run_worker.sh` launcher.

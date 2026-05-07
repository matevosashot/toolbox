# Worker example

A self-contained demo of the [tasker](../../docs/tasker.md): a launcher
script and four pre-seeded tasks that exercise every scheduling feature.

## Layout

```
examples/worker/
├── run_worker.sh          # seeds tasks/pending/ from tasks/draft/, then runs the worker
└── tasks/
    ├── .gitignore         # ignores all runtime dirs (pending/ running/ completed/ ...)
    └── draft/             # canonical task templates, checked into git
        ├── hello              # plain bash task
        ├── [3]heartbeat       # task array (runs 4× with $1 = 3,2,1,0)
        ├── !important         # priority task (claimed first)
        ├── will_fail          # exits non-zero → routed to failed/
        ├── iampython          # python via shebang (chmod +x, no extension)
        └── imustbepython.py   # python via .py extension (no chmod needed)
```

The last two demonstrate non-bash interpreters. The worker picks the
interpreter using, in order: `.py` extension → executable bit + shebang
→ bash. See [`docs/tasker.md` § How tasks are executed](../../docs/tasker.md#how-tasks-are-executed)
for the full table.

`tasks/pending/` is a runtime directory. `run_worker.sh` re-creates it
on every launch by copying `tasks/draft/*` into it, so the example is
fully reproducible without any git hygiene after a run.

## Run it

```bash
cd examples/worker
./run_worker.sh
```

You should see, roughly in order:

1. `!important` is claimed first because of the `!` prefix.
2. `hello` and `[3]heartbeat` are picked at random afterwards.
3. `will_fail` lands in `tasks/failed/` with `__7` appended to its name
   (the exit code).
4. The `[3]heartbeat` array fans out into `[2]`, `[1]`, `[0]`, each
   logging the current `N` value.
5. Once `pending/` empties the worker prints
   `No pending tasks. Sleeping for 3.0s...` and continues to poll.

Stop the worker with `Ctrl+C`.

## Inspect the results

```bash
ls tasks/completed/         # successful runs (with timestamp suffixes)
ls tasks/failed/            # failed runs (exit code in the name)
cat tasks/logs/*.log        # per-worker log
ls tasks/stdout/            # captured stdout/stderr per task
```

## Re-run

`run_worker.sh` re-seeds `pending/` from `draft/` on every launch, so
running it again Just Works:

```bash
./run_worker.sh
```

To start completely from scratch (drop completed/, failed/, logs/),
delete the runtime dirs first:

```bash
rm -rf tasks/{pending,running,completed,failed,logs,archive}
./run_worker.sh
```

To add a new sample task to the demo, drop a new file into
`tasks/draft/` — it will be picked up on the next launch.

## Pass-through arguments

Anything after the script name is forwarded to `toolbox worker`. For
example, to disable random selection so tasks are claimed in `ls`
order:

```bash
./run_worker.sh --no-random
```

To also forward error logs to Telegram (requires `TELEGRAM_BOT_TOKEN`):

```bash
./run_worker.sh --telegram
```

## See also

- [Full tasker documentation](../../docs/tasker.md) — file-name grammar,
  CLI reference, programmatic API, concurrency model.
- [`toolbox/tasker/worker.py`](../../toolbox/tasker/worker.py) — the
  implementation.

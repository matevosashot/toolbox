# Worker example

A self-contained demo of the [tasker](../../docs/tasker.md): a launcher
script and four pre-seeded tasks that exercise every scheduling feature.

## Layout

```
examples/worker/
├── run_worker.sh          # launches `toolbox worker -p ./tasks --loop ...`
└── tasks/
    ├── .gitignore         # ignores running/ completed/ failed/ logs/ archive/
    └── pending/
        ├── hello          # plain task
        ├── [3]heartbeat   # task array (runs 4× with $1 = 3,2,1,0)
        ├── !important     # priority task (claimed first)
        └── will_fail      # exits non-zero → routed to failed/
```

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
ls ~/worker_stdout/         # captured stdout/stderr per task
```

## Re-run

The launched worker drains `pending/`, so a second `./run_worker.sh`
finds nothing to do. Either restore the demo files from git
(`git checkout examples/worker/tasks/pending`) or copy any of the
existing scripts back into `pending/`:

```bash
cp tasks/completed/hello__*  tasks/pending/hello
```

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

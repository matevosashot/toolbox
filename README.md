# toolbox

A lightweight Python utility package for machine and git introspection.

## Table of Contents

- [Installation](#installation)
- [Functions](#functions)
  - [`git_info()`](#git_infodirectory-none)
  - [`get_launch_info()`](#get_launch_infostring-true)
  - [`get_hostname()`](#get_hostname)
  - [`get_local_ip()`](#get_local_ip)
- [CLI Scripts](#cli-scripts)
  - [`telesend`](#telesend)
  - [`telesend-data`](#telesend-data)
  - [`pingservers`](#pingservers)
  - [`teleserver`](#teleserver)
  - [`toolbox worker`](#toolbox-worker) — file-system task queue ([full docs](docs/tasker.md))
- [Path utilities](#path-utilities)
  - [`get_versions()`](#get_versionspath)
  - [`path_versioned()`](#path_versionedpath-versionnext-before_extensionfalse)
  - [`makedir_versioned()`](#makedir_versionedpath)
- [sys.path utilities](#syspath-utilities)
  - [`update()`](#updatedepth1-ancestor_namenone)
  - [`find_in_path()`](#find_in_pathpath)
- [Logging utilities](#logging-utilities)
  - [`setup_loggers()`](#setup_loggers)
  - [`report_errors()`](#report_errorsfunc--raise_errorfalse-logger)
  - [`get_telegram_handler()`](#get_telegram_handlerchat_id-)
  - [`get_file_handler()`](#get_file_handlerpathnone-levelloggininfo-modea)

---

## Installation

```bash
pip install git+https://github.com/matevosashot/toolbox
```

or (for development)

```bash
pip install -e .
```

## Functions

### `git_info(directory=None)`

Returns the current git branch and commit hash for a given directory.

```python
import toolbox

branch, commit = toolbox.git_info(directory="./")
# ('main', 'a3f5c2d...')
```

```bash
$ toolbox git_info ./
$ toolbox git_info --directory="./"
```

---

### `get_launch_info(string=True)`

Returns information about the current process: interpreter path, timestamp, command-line arguments, hostname, local IP, and git branch/commit if available.

```python
import toolbox

print(toolbox.get_launch_info())
# python ['script.py']
# 2026-03-26 10:00:00
# Env: /usr/bin/python3
# Git branch: main, a3f5c2d...
# Host: my-machine 192.168.1.10
```

Pass `string=False` to get a dict instead:

```python
info = toolbox.get_launch_info(string=False)
# {
#   'interpreter': '/usr/bin/python3',
#   'datetime': datetime(...),
#   'args': ['script.py'],
#   'hostname': 'my-machine',
#   'local_ip': '192.168.1.10',
#   'git_branch': 'main',
#   'git_commit': 'a3f5c2d...'
# }
```

---

### `get_hostname()`

Returns the hostname of the current machine.

```python
import toolbox

toolbox.get_hostname()
# 'my-machine'
```

---

### `get_local_ip()`

Returns the local IP address of the current machine.

```python
import toolbox

toolbox.get_local_ip()
# '192.168.1.10'
```

---

## CLI Scripts

### `telesend`

Sends a Telegram message from the command line. The message is prefixed with the current hostname and timestamp. Requires the `TELEGRAM_BOT_TOKEN` environment variable to be set.

```bash
telesend "deployment finished on node-3"
```

Output in Telegram:
```
*my-machine*  `2026-03-29 14:00:00` 🔵
deployment finished on node-3
```

---

### `telesend-data`

Sends a file (image, video, audio, or any document) to a Telegram chat. The endpoint is picked automatically from the file's MIME type:

| Extension / MIME | Endpoint | Compression |
| --- | --- | --- |
| `image/*` (jpg, png, …) | `sendPhoto` | Re-encoded by Telegram |
| `video/*` (mp4, …) | `sendVideo` | Re-encoded by Telegram |
| `audio/*` (mp3, wav, …) | `sendAudio` | Kept if compatible |
| anything else | `sendDocument` | None — byte-perfect |

Pass `--asfile` to force `sendDocument` for any input — useful for sending images or videos without Telegram's lossy re-encoding.

```bash
telesend-data <file> [caption...] [--asfile] [--no-header] [--chat_id=<id>]
```

| Argument         | Description                                                                                    |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| `file`           | Path to the file to send.                                                                      |
| `caption`        | Optional caption text (everything after the file path).                                        |
| `--asfile`       | Send as document, no compression, no data loss.                                                |
| `--no-header`    | Omit the `*hostname* *ip*` / timestamp header from the caption.                                |
| `--chat_id=<id>` | Destination chat. Named shortcut (`default`, `log`, `train`, `claude`) or a raw numeric id. Default: `default`. |

Requires the `TELEGRAM_BOT_TOKEN` environment variable to be set.

```bash
# Compressed photo with auto header + caption (goes to 'default')
telesend-data screenshot.png "loss curve at epoch 42"

# Lossless original PNG, no header
telesend-data screenshot.png --asfile --no-header

# Video — auto-detected, sent to the 'claude' chat
telesend-data demo.mp4 "build worked" --chat_id=claude

# Raw numeric chat id
telesend-data report.pdf --chat_id=-1001234567890
```

---

### `pingservers`

Continuously pings a list of servers at a given interval. If any server becomes unreachable, it sends a Telegram alert via `telesend` and exits.

```bash
pingservers [interval] <server1> <server2> [...]
```

| Argument   | Description                                      |
| ---------- | ------------------------------------------------ |
| `interval` | Optional. Ping interval in seconds (default: 60) |
| `server`   | One or more hostnames or IP addresses to monitor |

```bash
# Ping two servers every 30 seconds
pingservers 30 192.168.1.1 192.168.1.2

# Use default 60-second interval
pingservers my-server-1 my-server-2
```

---

### `teleserver`

A Telegram remote shell. Polls a Telegram channel for messages that start with a configurable prefix (`$` by default), executes them as shell commands on the local machine, and sends the output back to the channel.

```bash
toolbox teleserver [--chat_id log] [--prefix '$'] [--poll_interval 1] [--timeout 30] [--log_path ~/logs/]
```

| Argument | Default | Description |
| --- | --- | --- |
| `--chat_id` | `log` | Channel to listen on. Named shortcuts: `log`, `train`. Or a raw numeric ID. |
| `--token` | `$TELEGRAM_BOT_TOKEN` | Telegram bot token. |
| `--prefix` | `$` | Message prefix that marks a command. Both `$cmd` and `$ cmd` are accepted. |
| `--poll_interval` | `1.0` | Seconds between polling cycles. |
| `--timeout` | `30` | Maximum seconds a shell command may run before being killed. |
| `--log_path` | `~/logs/` | Directory (or `.log` file path) for log output. |

**Usage — in the Telegram channel:**

```
$ ls -la
$ df -h
$ cat /etc/hostname
```

**Sensitive-command guard**

Commands matching destructive patterns (`rm`, `dd`, `sudo`, `kill`, `shutdown`, `mv`, `chmod`, `chown`, etc.) are blocked on the first send. The bot replies asking you to send the exact same message again within 60 seconds to confirm:

```
You:  $ rm old_checkpoint.pt
Bot:  ⚠ WARNING: sensitive command detected.
      Send again to confirm (60s window):
      $ rm old_checkpoint.pt

You:  $ rm old_checkpoint.pt   ← confirmed, executes
```

Sending any other command before confirming cancels the pending operation silently.

---

### `toolbox worker`

Runs a [tasker](docs/tasker.md) worker — a file-system based task queue
where each task is a bash script in a shared `pending/` directory.
Workers race to claim scripts via atomic `os.rename`, run them, and
move them into `completed/` or `failed/` based on the exit code.

```bash
toolbox worker --task-base-path /shared/jobs --loop
```

| Argument            | Default              | Description |
| ------------------- | -------------------- | --- |
| `--task-base-path`, `-p` | `$TASK_BASE_PATH` | Root directory containing `pending/`, `running/`, … |
| `--worker-name`, `-n` | hostname           | Identifier embedded into running/completed file names. |
| `--loop`            | off                  | Poll forever instead of processing a single task and exiting. |
| `--no-random`       | off                  | Pick the first listed pending task instead of a random one. |
| `--idle-sleep`      | `10`                 | Seconds to sleep when the queue is empty. |
| `--failure-sleep`   | `2`                  | Seconds to sleep after a failed task. |
| `--restart-sleep`   | `5`                  | Seconds to sleep after an unhandled exception in `--loop` mode. |
| `--stdout-dir`      | `<task_base_path>/stdout/` | Directory for per-task stdout/stderr capture files. |
| `--log-path`        | `<task_base_path>/logs/<worker_name>.log` | Worker log file (or directory). |
| `--telegram`        | off                  | Forward `ERROR`-level log records to Telegram. |

For the file-name grammar (priority `!`, task arrays `[N]`,
non-propagating `*`), the on-disk layout, the programmatic API, and
the concurrency model, see the dedicated reference:

→ **[docs/tasker.md](docs/tasker.md)**

A runnable demo is in [`examples/worker/`](examples/worker/README.md).

---

## Path utilities

### `get_versions(path)`

Returns all existing versioned paths for a given base path. Matches both naming styles: `name_vN.ext` and `name.ext_vN`.

```python
from toolbox.path import get_versions

get_versions("./outputs/model.pt")
# ['./outputs/model_v1.pt', './outputs/model_v2.pt']
```

---

### `path_versioned(path, version="next", before_extension=False)`

Returns a versioned variant of the given path.

- `version="next"` — returns `path` unchanged if it does not exist; otherwise increments `_v1`, `_v2`, … until finding an unused path.
- `version=<int>` — returns the explicitly versioned path without any existence check.
- `before_extension=True` — inserts the version tag before the file extension (`name_vN.ext`); default is after (`name.ext_vN`).

```python
from toolbox.path import path_versioned

# Auto-increment (file already exists)
path_versioned("./outputs/model.pt")
# './outputs/model.pt_v1'

# Explicit version, tag before extension
path_versioned("./outputs/model.pt", version=3, before_extension=True)
# './outputs/model_v3.pt'
```

---

### `makedir_versioned(path)`

Creates and returns a versioned directory. If `path` does not exist it is created and returned as-is; otherwise the next unused versioned path is created and returned.

```python
from toolbox.path import makedir_versioned

makedir_versioned("./runs/experiment")
# './runs/experiment'        (created on first call)

makedir_versioned("./runs/experiment")
# './runs/experiment_v1'     (created on second call)
```

---

## sys.path utilities

Helpers for manipulating and searching the Python import path. The module mirrors stdlib `sys.path` semantics and is accessed as `toolbox.sys.path.<verb>`.

### `update(depth=1, ancestor_name=None)`

Inserts an ancestor directory of the *caller's* file at the front of `sys.path`. Useful for bootstrapping imports in scripts that aren't launched from the project root.

The caller's file is located via stack introspection (`sys._getframe` with an `inspect.stack` fallback), so the function takes no path argument — it always operates relative to whoever called it.

- `depth=N` — ascend `N` directory levels above the caller's file. `depth=0` adds the directory directly containing the caller; `depth=1` (the default) adds its parent.
- `ancestor_name="my_project"` — ascend until a directory with that basename is reached. Raises `ValueError` if none is found before the filesystem root. When given, `depth` is ignored.

Returns the directory that was inserted.

```python
import toolbox.sys.path

# Project layout:
#   /repos/my_project/
#       my_project/             <- importable package
#       scripts/sub/
#           train.py            <- this file calling update()

toolbox.sys.path.update(depth=2)
# '/repos/my_project'
# sys.path[0] is now '/repos/my_project', so `import my_project` works.

toolbox.sys.path.update(ancestor_name="my_project")
# Same result, but robust to scripts being moved deeper in the tree.
```

---

### `find_in_path(path)`

Locates a file or directory either at the given path or under any `sys.path` entry, and returns its absolute path.

Resolution order:

1. If `path` exists as given (absolute, or relative to the current working directory), its absolute form is returned.
2. If `path` is absolute and does not exist, `ValueError` is raised — an absolute path cannot be meaningfully resolved against `sys.path`.
3. Otherwise each entry of `sys.path` is joined with `path` in order and the first existing result is returned as an absolute path.

```python
from toolbox.sys.path import find_in_path

find_in_path("configs/default.yaml")
# '/repos/my_project/configs/default.yaml'

find_in_path("/etc/hosts")
# '/etc/hosts'                  (existing absolute path returned as-is)

find_in_path("nope.txt")
# FileNotFoundError: Path 'nope.txt' not found in sys.path
```

---

## Logging utilities

### `setup_loggers(...)`

Configures and returns the root `main` logger. A file handler is always attached; additional handlers are enabled via flags.

```python
from toolbox import setup_loggers

logger = setup_loggers(
    base_path="./logs",   # directory or .log file path for the main log
    debug=True,           # also write DEBUG records to ~/logs/debug.log
    telegram=True,        # forward WARNING+ to the Telegram "log" channel
    train_logger=True,    # set up main.train → Telegram "train" channel
    stdout=True,          # mirror output to stdout
)

logger.info("Training started")
```


| Parameter      | Type   | Default | Description                                    |
| -------------- | ------ | ------- | ---------------------------------------------- |
| `base_path`    | `str   | None`   | `~/logs/`                                      |
| `debug`        | `bool` | `False` | Enable verbose debug log at `~/logs/debug.log` |
| `telegram`     | `bool` | `False` | Send WARNING+ records to Telegram              |
| `train_logger` | `bool` | `True`  | Configure `main.train` child logger            |
| `stdout`       | `bool` | `False` | Echo records to stdout                         |


---

### `report_errors(func, *, raise_error=False, logger=...)`

Decorator that catches exceptions, logs them via the `main` logger, and optionally re-raises. Supports both bare and parameterised usage.

```python
from toolbox import report_errors

@report_errors
def risky():
    ...  # exceptions are logged, execution continues

@report_errors(raise_error=True)
def strict():
    ...  # exceptions are logged and then re-raised
```

---

### `get_telegram_handler(chat_id, ...)`

Creates a `[TelegramHandler](https://github.com/sashel/telegram-handler)` that posts log records to a Telegram chat. `chat_id` can be a raw integer chat ID or a named shortcut (`"log"` or `"train"`).

```python
from toolbox import get_telegram_handler
import logging

handler = get_telegram_handler(
    chat_id="log",
    level=logging.WARNING,
    disable_notification=False,
    emoji=False,
)
logging.getLogger("main").addHandler(handler)
```

The bot token is read from the `TELEGRAM_BOT_TOKEN` environment variable.

---

### `get_file_handler(path=None, level=logging.INFO, mode='a')`

Creates a `logging.FileHandler` with a detailed formatter. If `path` is a directory, the filename is auto-generated as `log@<ip>.log`; if it ends with `.log` it is used as-is.

```python
from toolbox import get_file_handler
import logging

logging.getLogger("main").addHandler(get_file_handler("./logs", level=logging.DEBUG))
```


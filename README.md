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
  - [`pingservers`](#pingservers)
  - [`teleserver`](#teleserver)
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


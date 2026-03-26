# toolbox

A lightweight Python utility package for machine and git introspection.

## Installation

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

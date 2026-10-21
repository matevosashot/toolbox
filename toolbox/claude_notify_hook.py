"""Claude Code hook: Telegram notifications for long turns and input requests.

Usage (hook JSON is read from stdin)::

    toolbox claude_notify_hook start    # UserPromptSubmit: remember prompt time
    toolbox claude_notify_hook stop     # Stop: notify if the turn took > threshold
    toolbox claude_notify_hook notify   # Notification: Claude needs your input

Config precedence: CLI flags > JSON config file > defaults.
"""
import argparse
import asyncio
import json
import os
import sys
import time

from telegram import Bot

from toolbox.configs.telegram import TELEGRAM_BOT_TOKEN
from toolbox.machine import get_hostname
from toolbox.telesend_data import _md_escape, _resolve_chat_id

DEFAULT_CONFIG_PATH = "~/.claude/claude_notify_hook.json"
DEFAULTS = {"threshold": 60, "exclude_dirs": [], "chat": "claude"}


def _start_file(session_id):
    return "/tmp/claude_start_%s" % session_id


def load_config(args):
    """Merge defaults <- config file <- CLI flags."""
    cfg = {"threshold": DEFAULTS["threshold"], "exclude_dirs": [], "chat": DEFAULTS["chat"]}
    path = os.path.expanduser(args.config or DEFAULT_CONFIG_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            file_cfg = json.load(fh)
        if isinstance(file_cfg, dict):
            if "threshold" in file_cfg:
                cfg["threshold"] = float(file_cfg["threshold"])
            if "chat" in file_cfg:
                cfg["chat"] = str(file_cfg["chat"])
            if isinstance(file_cfg.get("exclude_dirs"), list):
                cfg["exclude_dirs"] = [str(d) for d in file_cfg["exclude_dirs"]]
    except (OSError, ValueError):
        pass
    if args.threshold is not None:
        cfg["threshold"] = args.threshold
    if args.chat:
        cfg["chat"] = args.chat
    cfg["exclude_dirs"] = cfg["exclude_dirs"] + list(args.exclude or [])
    return cfg


def is_excluded(cwd, exclude_dirs):
    cwd = os.path.realpath(cwd)
    for d in exclude_dirs:
        d = os.path.realpath(os.path.expanduser(d))
        if os.path.commonpath([cwd, d]) == d:
            return True
    return False


def session_title(transcript_path):
    """User-set title (/rename) wins over the auto-generated one."""
    custom = auto = ""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                if '-title"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") == "custom-title" and obj.get("customTitle"):
                    custom = obj["customTitle"]
                elif obj.get("type") == "ai-title" and obj.get("aiTitle"):
                    auto = obj["aiTitle"]
    except OSError:
        pass
    return custom or auto or "untitled"


def fmt_duration(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return " ".join(p for p in ("%dh" % h if h else "", "%dm" % m if m else "", "%ds" % s) if p)


def build_message(icon, state, title, cwd, session_id, elapsed=None):
    lines = ["%s *%s*" % (icon, _md_escape(title)), _md_escape(state)]
    if elapsed is not None:
        lines.append("*Duration:* `%s`" % fmt_duration(elapsed))
    lines += [
        "*Dir:* `%s`" % cwd,
        "*Session:* `%s`" % session_id,
        "*Host:* %s" % _md_escape(get_hostname()),
    ]
    return "\n".join(lines)


async def _send(chat, text):
    chat_id, thread_id = _resolve_chat_id(chat)
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if thread_id is not None:
        kwargs["message_thread_id"] = int(thread_id)
    async with Bot(TELEGRAM_BOT_TOKEN) as bot:
        await bot.send_message(**kwargs)


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="toolbox claude_notify_hook", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["start", "stop", "notify"])
    p.add_argument("--threshold", type=float, default=None, metavar="SECONDS",
                   help="min turn duration for 'stop' notifications (default 60)")
    p.add_argument("--exclude", action="append", metavar="DIR",
                   help="skip sessions whose cwd is DIR or inside it (repeatable; "
                        "added to the config file's exclude_dirs)")
    p.add_argument("--chat", default=None, metavar="NAME",
                   help="telegram chat shortcut or id (default 'claude')")
    p.add_argument("--config", default=None, metavar="PATH",
                   help="JSON config file (default %s)" % DEFAULT_CONFIG_PATH)
    return p.parse_args(argv)


def _run(args):
    data = json.load(sys.stdin)
    session_id = data.get("session_id", "unknown")
    path = _start_file(session_id)

    if args.mode == "start":
        with open(path, "w") as fh:
            fh.write(str(time.time()))
        return

    cfg = load_config(args)
    cwd = data.get("cwd") or os.getcwd()
    if is_excluded(cwd, cfg["exclude_dirs"]):
        return
    title = session_title(data.get("transcript_path", ""))

    if args.mode == "notify":
        text = build_message("⏳", "input needed", title, cwd, session_id)
    else:
        if data.get("stop_hook_active"):
            return
        try:
            with open(path) as fh:
                elapsed = time.time() - float(fh.read())
        except (OSError, ValueError):
            return
        if elapsed <= cfg["threshold"]:
            return
        text = build_message("✅", "finished", title, cwd, session_id, elapsed)
    asyncio.run(_send(cfg["chat"], text))


def main():
    args = _parse_args(sys.argv[1:])
    try:
        _run(args)
    except Exception as exc:  # never block Claude
        print("claude_notify_hook: %s" % exc, file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()

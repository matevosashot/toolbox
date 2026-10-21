"""Claude Code hook: Telegram notifications for long turns and input requests.

Usage (hook JSON is read from stdin)::

    toolbox claude_notify_hook start    # UserPromptSubmit: remember prompt time
    toolbox claude_notify_hook stop     # Stop: notify if the turn took > threshold
    toolbox claude_notify_hook notify   # Notification: Claude needs your input
    toolbox claude_notify_hook statusline  # statusLine: records cost/model for the hook

The statusline command receives ``cost.total_cost_usd`` and the model from Claude
Code (hooks don't), stores them in /tmp and prints a short status line.

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


def _state_file(session_id):
    return "/tmp/claude_state_%s.json" % session_id


def _read_state(session_id):
    """Latest {cost, model} written by the statusline command ({} if unavailable)."""
    try:
        with open(_state_file(session_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


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


def transcript_info(transcript_path):
    """Return (title, remote_url) from the transcript.

    Title: the /rename name if set, else Claude's auto-generated one, else "untitled".
    URL: the claude.ai/code link when Remote Control has been on for the session
    (transcript has a ``bridge-session`` entry), else "".
    """
    custom = auto = bridge = ""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                if '-title"' not in line and '"bridge-session"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                kind = obj.get("type")
                if kind == "custom-title" and obj.get("customTitle"):
                    custom = obj["customTitle"]
                elif kind == "ai-title" and obj.get("aiTitle"):
                    auto = obj["aiTitle"]
                elif kind == "bridge-session" and obj.get("bridgeSessionId"):
                    bridge = obj["bridgeSessionId"]
    except OSError:
        pass
    url = ""
    if bridge:
        url = "https://claude.ai/code/session_%s" % bridge.split("_", 1)[-1]
    return custom or auto or "untitled", url


def fmt_duration(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return " ".join(p for p in ("%dh" % h if h else "", "%dm" % m if m else "", "%ds" % s) if p)


def fmt_cost(cost, baseline):
    if cost is None:
        return ""
    if baseline is None:
        return "$%.2f" % cost
    return "$%.2f($%.2f)" % (max(cost - baseline, 0.0), cost)


def build_message(icon, state, title, cwd, session_id, elapsed=None, cost="", model="", url=""):
    head = "%s *%s*" % (icon, _md_escape(title))
    if state:
        head += " \u2014 %s" % _md_escape(state)
    info = " \u00b7 ".join(
        p for p in (fmt_duration(elapsed) if elapsed is not None else "", cost,
                    _md_escape(model), _md_escape(get_hostname())) if p)
    lines = [head, info, "`%s`" % cwd, "`%s`" % session_id]
    if url:
        lines.append("[open](%s)" % url)
    return "\n".join(lines)


def statusline(data):
    """statusLine command: persist cost/model for the hooks, print a status line."""
    sid = data.get("session_id", "unknown")
    cost = (data.get("cost") or {}).get("total_cost_usd")
    model = (data.get("model") or {}).get("display_name") or ""
    try:
        with open(_state_file(sid), "w", encoding="utf-8") as fh:
            json.dump({"cost": cost, "model": model}, fh)
    except OSError:
        pass
    print("%s \u00b7 %s" % (model, "$%.2f" % cost if cost is not None else "$-"))


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
    p.add_argument("mode", choices=["start", "stop", "notify", "statusline"])
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
    if args.mode == "statusline":
        return statusline(data)

    session_id = data.get("session_id", "unknown")
    path = _start_file(session_id)

    if args.mode == "start":
        with open(path, "w") as fh:
            json.dump({"t": time.time(), "cost": _read_state(session_id).get("cost")}, fh)
        return

    cfg = load_config(args)
    cwd = data.get("cwd") or os.getcwd()
    if is_excluded(cwd, cfg["exclude_dirs"]):
        return
    if args.mode == "stop" and data.get("stop_hook_active"):
        return
    try:
        with open(path) as fh:
            start = json.load(fh)
        elapsed = time.time() - float(start["t"])
    except (OSError, ValueError, KeyError, TypeError):
        if args.mode == "stop":
            return
        start, elapsed = {}, None
    if args.mode == "stop" and elapsed <= cfg["threshold"]:
        return

    state = _read_state(session_id)
    cost = fmt_cost(state.get("cost"), start.get("cost"))
    title, url = transcript_info(data.get("transcript_path", ""))
    icon, label = ("✅", "") if args.mode == "stop" else ("⏳", "input needed")
    text = build_message(icon, label, title, cwd, session_id, elapsed, cost,
                         state.get("model", ""), url)
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

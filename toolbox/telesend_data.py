from __future__ import annotations

import mimetypes
import sys
from datetime import datetime
from pathlib import Path

import requests

from toolbox.configs.telegram import CHANNEL_IDS, TELEGRAM_BOT_TOKEN
from toolbox.machine import get_hostname, get_local_ip

CHAT_ID = CHANNEL_IDS["log"]
CAPTION_LIMIT = 1024

ENDPOINTS = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "audio": ("sendAudio", "audio"),
    "document": ("sendDocument", "document"),
}


def _pick_kind(path: Path, asfile: bool) -> str:
    if asfile:
        return "document"
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        major = mime.split("/", 1)[0]
        if major in ("image", "video", "audio"):
            return {"image": "photo"}.get(major, major)
    return "document"


def _build_caption(user_caption: str, no_header: bool) -> str:
    if no_header:
        caption = user_caption
    else:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"*{get_hostname()}* *{get_local_ip()}* 🔵\n`{ts}`"
        caption = f"{header}\n{user_caption}" if user_caption else header
    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 1] + "…"
    return caption


USAGE = (
    "Usage: telesend-data <file> [caption...] [--asfile] [--no-header]\n"
    "Send an image / video / audio / file to the Telegram log channel.\n"
    "  --asfile     send as document (no compression, no data loss)\n"
    "  --no-header  omit the hostname/timestamp caption header"
)


def _parse_argv(argv: list[str]) -> tuple[str, str, bool, bool]:
    asfile = no_header = False
    positional: list[str] = []
    for tok in argv:
        if tok in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)
        elif tok == "--asfile":
            asfile = True
        elif tok == "--no-header":
            no_header = True
        else:
            positional.append(tok)
    if not positional:
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    return positional[0], " ".join(positional[1:]), asfile, no_header


def main() -> None:
    file_arg, caption_arg, asfile, no_header = _parse_argv(sys.argv[1:])

    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    path = Path(file_arg)
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    kind = _pick_kind(path, asfile)
    method, field = ENDPOINTS[kind]
    caption = _build_caption(caption_arg, no_header)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
    with path.open("rb") as fh:
        files = {field: (path.name, fh)}
        resp = requests.post(url, data=data, files=files, timeout=120)

    try:
        body = resp.json()
    except ValueError:
        print(f"HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    if not body.get("ok"):
        print(f"Telegram error: {body}", file=sys.stderr)
        sys.exit(1)

    print(f"sent via {method} ({path.name})")


if __name__ == "__main__":
    main()

"""Send a file (photo/video/audio/document) or a plain text message to a
Telegram chat from the command line.

If the first positional argument is an existing file it is uploaded with the
appropriate method; otherwise all positional arguments are joined and sent as a
text message.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import sys
from datetime import datetime
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from toolbox.configs.telegram import CHANNEL_IDS, TELEGRAM_BOT_TOKEN
from toolbox.machine import get_hostname, get_local_ip

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

# kind -> (Bot method name, keyword for the file payload)
ENDPOINTS = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "audio": ("send_audio", "audio"),
    "document": ("send_document", "document"),
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


def _md_escape(text: str) -> str:
    for ch in "_*`[":
        text = text.replace(ch, "\\" + ch)
    return text


def _build_caption(user_caption: str, no_header: bool, limit: int = CAPTION_LIMIT) -> str:
    safe_caption = _md_escape(user_caption)
    if no_header:
        caption = safe_caption
    else:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"*{get_hostname()}* *{get_local_ip()}* 🔵\n`{ts}`"
        caption = f"{header}\n{safe_caption}" if safe_caption else header
    if len(caption) > limit:
        caption = caption[: limit - 1] + "…"
    return caption


def _resolve_chat_id(value: str) -> tuple[str, str | None]:
    """Resolve a ``<chat>[:<thread>]`` target to ``(chat_id_str, thread_id_or_None)``.

    ``<chat>`` may be a named shortcut from :data:`CHANNEL_IDS` or a raw numeric
    id; the optional ``<thread>`` suffix is a Telegram topic id.
    """
    chat_part, sep, thread_part = value.partition(":")
    embedded_thread = thread_part if sep and thread_part else None
    if chat_part in CHANNEL_IDS:
        return str(CHANNEL_IDS[chat_part]), embedded_thread
    try:
        int(chat_part)
    except ValueError:
        raise SystemExit(
            f"Unknown chat_id '{chat_part}'. "
            f"Use a numeric id or one of: {', '.join(CHANNEL_IDS)}"
        )
    return chat_part, embedded_thread


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telesend-data",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "content",
        nargs="+",
        metavar="<file|text> [caption...]",
        help="A file path to upload, or text to send. Extra args become the "
        "caption (for files) or are appended to the text message.",
    )
    parser.add_argument(
        "--asfile",
        action="store_true",
        help="Send as a document (no compression, no data loss).",
    )
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="Omit the hostname/timestamp caption header.",
    )
    parser.add_argument(
        "--chat_id",
        default="default",
        metavar="<id>",
        help=f"Named shortcut ({', '.join(CHANNEL_IDS)}) or raw numeric id; may "
        "embed a topic as '<id>:<thread_id>' (default: 'default').",
    )
    parser.add_argument(
        "--thread_id",
        default=None,
        metavar="<id>",
        help="Message thread (topic) id; overrides any ':<thread_id>' embedded "
        "in --chat_id.",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="<token>",
        help="Telegram bot token (default: TELEGRAM_BOT_TOKEN env var).",
    )
    parser.add_argument(
        "--claude_session",
        default=None,
        metavar="<session_id>",
        help="Attach a 'claude' inline button with callback data "
        "'claude:<session_id>'.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, token: str) -> int:
    resolved_chat, embedded_thread = _resolve_chat_id(args.chat_id)
    # An explicit --thread_id flag wins over a ':<thread>' embedded in --chat_id.
    thread_id = args.thread_id if args.thread_id is not None else embedded_thread
    common = {"chat_id": resolved_chat, "parse_mode": "Markdown"}
    if thread_id is not None:
        common["message_thread_id"] = int(thread_id)
    if args.claude_session:
        common["reply_markup"] = InlineKeyboardMarkup(
            [[InlineKeyboardButton("claude", callback_data=f"claude:{args.claude_session}")]]
        )

    file_arg = args.content[0]
    extra = " ".join(args.content[1:])
    path = Path(file_arg)

    async with Bot(token) as bot:
        if file_arg and path.is_file():
            kind = _pick_kind(path, args.asfile)
            method_name, field = ENDPOINTS[kind]
            send = getattr(bot, method_name)
            with path.open("rb") as fh:
                await send(
                    caption=_build_caption(extra, args.no_header),
                    **{field: fh},
                    **common,
                )
            print(f"sent via {method_name} ({path.name})")
            return 0

        text_body = " ".join(t for t in (file_arg, extra) if t)
        if not text_body:
            print("Nothing to send: provide a file path or text.", file=sys.stderr)
            return 2
        await bot.send_message(
            text=_build_caption(text_body, args.no_header, TEXT_LIMIT),
            **common,
        )
        print("sent via send_message (text)")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    token = args.token or TELEGRAM_BOT_TOKEN
    if not token:
        print("No bot token: pass --token=<token> or set TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_run(args, token))
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

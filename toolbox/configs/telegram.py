from __future__ import annotations

import os
from typing import Optional

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_IDS: dict[str, int] = {
    "default": -1001382816694,
    "log":     -1001382816694,
    "bot": 278564137,
    "train": -1003768125381,
    "claude": -1003645727330,
}


def resolve_chat_target(value) -> tuple[int, Optional[int]]:
    """Split a ``<chat>:<thread>`` target into ``(chat_id, thread_id)``.

    ``<chat>`` may be a named shortcut from :data:`CHANNEL_IDS` or a raw numeric
    id; ``<thread>`` is an optional Telegram ``message_thread_id`` (topic). Named
    shortcuts and negative numeric ids never contain ``:``, so splitting on the
    first colon is safe.
    """
    chat_part, sep, thread_part = str(value).partition(":")
    chat_id = int(CHANNEL_IDS.get(chat_part, chat_part))
    thread_id = int(thread_part) if sep and thread_part else None
    return chat_id, thread_id

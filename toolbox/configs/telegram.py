from __future__ import annotations

import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_IDS: dict[str, int] = {
    "default": -1001382816694,
    "log":     -1001382816694,
    "bot": 278564137,
    "train": -1003768125381,
    "claude": -1003645727330,
}

from __future__ import annotations

import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_IDS: dict[str, int] = {
    "log": -1001382816694,
    "train": 278564137,
}

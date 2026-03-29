import logging
import logging.config
import os
import random
from typing import Optional, Union

from toolbox.machine import get_local_ip

import telegram_handler
import telegram_handler.formatters
from telegram_handler import TelegramHandler, MarkdownFormatter, TelegramFormatter, HtmlFormatter


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_IDS: dict[str, int] = {
    "log": -1001382816694,
    "train": 278564137,
}


class EMOJI:
    """Unicode emoji constants for use in Telegram log message formatting."""

    # Circles
    WHITE_CIRCLE = '\U000026AA'
    BLACK_CIRCLE = '\U000026AB'
    BLUE_CIRCLE = '\U0001F535'
    RED_CIRCLE = '\U0001F534'
    GREEN_CIRCLE = '\U0001F7E2'
    YELLOW_CIRCLE = '\U0001F7E1'
    PURPLE_CIRCLE = '\U0001F7E3'
    ORANGE_CIRCLE = '\U0001F7E0'
    BROWN_CIRCLE = '\U0001F7E4'

    # Squares
    WHITE_SQUARE = '\U00002B1C'
    BLACK_SQUARE = '\U00002B1B'
    RED_SQUARE = '\U0001F7E5'
    BLUE_SQUARE = '\U0001F7E6'
    GREEN_SQUARE = '\U0001F7E9'
    YELLOW_SQUARE = '\U0001F7E8'
    PURPLE_SQUARE = '\U0001F7EA'
    ORANGE_SQUARE = '\U0001F7E7'
    BROWN_SQUARE = '\U0001F7EB'

    # Stars
    STAR = '\U00002B50'

    # Hearts
    RED_HEART = '\U00002764'
    BLUE_HEART = '\U0001F499'
    GREEN_HEART = '\U0001F49A'
    YELLOW_HEART = '\U0001F49B'
    PURPLE_HEART = '\U0001F49C'
    ORANGE_HEART = '\U0001F9E1'
    BLACK_HEART = '\U0001F5A4'
    WHITE_HEART = '\U0001F90D'
    BROWN_HEART = '\U0001F90E'

    # Misc
    FIRE = '\U0001F525'
    THUMBS_UP = '\U0001F44D'
    SKULL = '\U0001F480'
    ROCKET = '\U0001F680'


def get_telegram_handler(
    chat_id: Union[int, str],
    token: Optional[str] = TELEGRAM_BOT_TOKEN,
    level: int = logging.INFO,
    disable_notification: Optional[bool] = None,
    emoji: bool = False,
) -> TelegramHandler:
    """Create a :class:`TelegramHandler` that posts log records to a Telegram chat.

    *chat_id* can be a raw Telegram chat/channel ID or one of the named shortcuts
    defined in :data:`CHANNEL_IDS` (``"log"`` or ``"train"``).

    The message format includes the local IP suffix so it's easy to identify
    which machine sent the record.  When *emoji* is True a random coloured
    symbol is prepended instead of a timestamp (useful for training-progress
    channels where brevity matters).

    When *disable_notification* is not supplied it defaults to ``False`` for
    INFO+ records and ``True`` for lower levels (DEBUG/NOTSET).

    Args:
        chat_id: Telegram chat ID or a key from :data:`CHANNEL_IDS`.
        token: Telegram Bot API token. Falls back to the ``TELEGRAM_BOT_TOKEN``
            environment variable.
        level: Minimum logging level forwarded to Telegram.
        disable_notification: Silence push notifications on the receiving device.
            If ``None``, derived automatically from *level*.
        emoji: Use a random emoji prefix instead of a timestamp in the message.

    Returns:
        A configured :class:`TelegramHandler` instance.

    Raises:
        ValueError: If *token* is ``None`` and the environment variable is unset.
    """
    if token is None:
        raise ValueError(
            "Token is required. It can be read from the TELEGRAM_BOT_TOKEN "
            "environment variable or passed explicitly."
        )

    if chat_id in CHANNEL_IDS:
        chat_id = CHANNEL_IDS[chat_id]

    if disable_notification is None:
        disable_notification = level < logging.INFO

    local_ip = get_local_ip().replace("192.168", "")
    if emoji:
        random_emoji = random.choice(list(vars(EMOJI).values()))
        fmt = f'{random_emoji} *%(levelname)s* `{local_ip}`\n%(message)s'
    else:
        fmt = f'`%(asctime)s` *%(levelname)s* `{local_ip}`\n[%(name)s:%(funcName)s]\n%(message)s'

    datefmt = '%Y-%m-%d %H:%M:%S'

    handler = TelegramHandler(
        token=token,
        chat_id=chat_id,
        level=level,
        disable_notification=disable_notification,
    )
    handler.setFormatter(MarkdownFormatter(fmt, datefmt=datefmt))
    return handler

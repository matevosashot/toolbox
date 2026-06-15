import logging
import logging.config
import os
import random
from typing import Optional, Union

from toolbox.machine import get_local_ip

import telegram_handler
import telegram_handler.formatters
from telegram_handler import TelegramHandler, MarkdownFormatter, TelegramFormatter, HtmlFormatter


from toolbox.configs.telegram import TELEGRAM_BOT_TOKEN, CHANNEL_IDS, resolve_chat_target
from toolbox.configs.emoji import EMOJI


class _ThreadAwareTelegramHandler(TelegramHandler):
    """:class:`TelegramHandler` understanding a combined ``<chat>:<thread>`` id.

    The vendored ``telegram_handler.TelegramHandler`` has no
    ``message_thread_id`` support and treats ``chat_id`` opaquely, so split the
    chat id at the only point it matters — the outgoing request.
    """

    @staticmethod
    def _split(kwargs: dict) -> dict:
        if "chat_id" in kwargs:
            cid, thread_id = resolve_chat_target(kwargs["chat_id"])
            kwargs["chat_id"] = cid
            if thread_id is not None:
                kwargs.setdefault("message_thread_id", thread_id)
        return kwargs

    def send_message(self, text, **kwargs):
        return super().send_message(text, **self._split(kwargs))

    def send_document(self, text, document, **kwargs):
        return super().send_document(text, document, **self._split(kwargs))


def get_telegram_handler(
    chat_id: Union[int, str],
    token: Optional[str] = TELEGRAM_BOT_TOKEN,
    level: int = logging.INFO,
    disable_notification: Optional[bool] = None,
    emoji: bool = False,
) -> TelegramHandler:
    """Create a :class:`TelegramHandler` that posts log records to a Telegram chat.

    *chat_id* can be a raw Telegram chat/channel ID or one of the named shortcuts
    defined in :data:`CHANNEL_IDS` (``"log"`` or ``"train"``). Append
    ``":<thread_id>"`` (e.g. ``"log:42"``) to post into a specific topic/thread.

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

    if disable_notification is None:
        disable_notification = level < logging.INFO

    local_ip = get_local_ip().replace("192.168", "")
    if emoji:
        random_emoji = random.choice(list(EMOJI))
        fmt = f'{random_emoji} *%(levelname)s* `{local_ip}`\n%(message)s'
    else:
        fmt = f'`%(asctime)s` *%(levelname)s* `{local_ip}`\n[%(name)s:%(funcName)s]\n%(message)s'

    datefmt = '%Y-%m-%d %H:%M:%S'

    handler = _ThreadAwareTelegramHandler(
        token=token,
        chat_id=chat_id,
        level=level,
        disable_notification=disable_notification,
    )
    handler.setFormatter(MarkdownFormatter(fmt, datefmt=datefmt))
    return handler

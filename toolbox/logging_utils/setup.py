import logging
import os
from typing import Optional

from .telegram_logger import get_telegram_handler
from .utils import get_file_handler


def setup_loggers(
    base_path: Optional[str] = None,
    debug: bool = False,
    telegram: bool = False,
    train_logger: bool = True,
    stdout: bool = False,
) -> logging.Logger:
    """Configure and return the root ``main`` logger.

    Always attaches a file handler writing INFO-level records to *base_path*.
    Additional handlers are added depending on the flags:

    * **debug** – adds a DEBUG-level file handler at ``~/logs/debug.log``.
    * **telegram** – adds a Telegram handler (WARNING+) posting to the ``log`` channel.
    * **train_logger** – configures a child logger ``main.train`` with a Telegram
      handler (INFO+) posting to the ``train`` channel with an emoji prefix.
    * **stdout** – adds a :class:`~logging.StreamHandler` to echo records to stdout.

    Args:
        base_path: Directory (or ``.log`` file path) for the main log file.
            Defaults to ``~/logs/``.
        debug: Whether to enable a verbose debug log file.
        telegram: Whether to forward WARNING+ records to the Telegram log channel.
        train_logger: Whether to set up the ``main.train`` child logger.
        stdout: Whether to mirror log output to stdout.

    Returns:
        The configured ``main`` :class:`logging.Logger`.
    """
    logger = logging.getLogger("main")
    for handler in logger.handlers:
        logger.removeHandler(handler)

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    logger.addHandler(get_file_handler(path=base_path, level=logging.INFO, mode="a"))

    if debug:
        os.makedirs(os.path.expanduser("~/logs/"), exist_ok=True)
        path = os.path.expanduser("~/logs/debug.log")
        logger.addHandler(get_file_handler(path=path, level=logging.DEBUG, mode="a"))

    if telegram:
        logger.addHandler(
            get_telegram_handler(chat_id="log", level=logging.WARNING, disable_notification=False)
        )

    if train_logger:
        train_log = logging.getLogger("train")
        for handler in train_log.handlers:
            train_log.removeHandler(handler)
        train_log.setLevel(logging.INFO)
        train_log.addHandler(
            get_telegram_handler(chat_id="train", level=logging.INFO, disable_notification=True, emoji=True)
        )

    if stdout:
        stdout_handler = logging.StreamHandler()
        stdout_handler.setLevel(logging.INFO)
        if debug:
            stdout_handler.setLevel(logging.DEBUG)
        logger.addHandler(stdout_handler)

    return logger

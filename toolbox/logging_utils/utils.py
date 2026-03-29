import logging
import os
import time
from typing import Callable, Optional

from toolbox.machine import get_local_ip

LOG_BASE_PATH = os.path.expanduser("~/logs/")


def report_errors(
    func: Optional[Callable] = None,
    *,
    raise_error: bool = False,
    logger: logging.Logger = logging.getLogger("main"),
) -> Callable:
    """Decorator that catches and logs exceptions without stopping execution.

    Can be applied with or without arguments::

        @report_errors
        def my_func(): ...

        @report_errors(raise_error=True)
        def my_func(): ...

    Args:
        func: The function to wrap (supplied automatically when used without parentheses).
        raise_error: If True, re-raises the exception after logging it.
        logger: Logger instance used to record the error.

    Returns:
        The wrapped function (or a decorator if *func* is None).
    """
    if func is None:
        return lambda f: report_errors(f, raise_error=raise_error, logger=logger)

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if not isinstance(e, KeyboardInterrupt):
                logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
                time.sleep(3)  # Give logger time to send message
                               # and avoid multiple messages in a short time

            if raise_error:
                raise e

    return wrapper


def get_file_handler(
    path: Optional[str] = None,
    level: int = logging.INFO,
    mode: str = "a",
) -> logging.FileHandler:
    """Create a :class:`logging.FileHandler` with a standard formatter.

    If *path* is a directory (i.e. does not end with ``.log``), a filename of
    the form ``log@<local_ip_suffix>.log`` is generated inside that directory.
    If *path* ends with ``.log`` it is used as-is and its parent directory is
    created when necessary.

    Args:
        path: Destination file path or directory. Defaults to ``~/logs/``.
        level: Logging level for the handler (e.g. ``logging.INFO``).
        mode: File open mode passed to :class:`~logging.FileHandler`.

    Returns:
        A configured :class:`logging.FileHandler`.
    """
    if path is None:
        path = LOG_BASE_PATH

    if path.endswith(".log"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        local_ip = get_local_ip().replace("192.168.", "")
        file = f"log@{local_ip}.log"
        os.makedirs(path, exist_ok=True)
        path = os.path.join(path, file)

    file_handler = logging.FileHandler(path, mode=mode)
    file_handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)-15s - %(levelname)-8s - pid:%(process)-5d  [%(filename)-20s:%(lineno)-4d - %(funcName)-20s] - %(message)s"
    )
    file_handler.setFormatter(formatter)

    return file_handler

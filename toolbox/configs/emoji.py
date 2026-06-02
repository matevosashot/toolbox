from enum import Enum


class EMOJI(str, Enum):
    """Unicode emoji constants for use in Telegram log message formatting.

    Each member is a ``str`` subclass, so it interpolates as the glyph in
    f-strings and concatenates with other strings transparently. Iterating
    the class (``list(EMOJI)``) yields only the declared members, which makes
    it safe to use with ``random.choice`` and similar helpers.
    """

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

    def __str__(self) -> str:
        # Without this, ``str(EMOJI.BLUE_CIRCLE)`` returns ``"EMOJI.BLUE_CIRCLE"``
        # on Python < 3.11, which would re-introduce a broken prefix in logs.
        return self.value

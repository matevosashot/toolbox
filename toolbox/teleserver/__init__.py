"""Reusable Telegram-bot building blocks for shell access from a chat.

Each feature is its own extension class that registers handlers on a
`telegram.ext.Application` you already own:

    from telegram.ext import ApplicationBuilder
    from toolbox.teleserver import ProcessExtension, TmuxExtension, ShellExtension

    app = ApplicationBuilder().token(TOKEN).build()
    ShellExtension(app, chat_id=MY_CHAT)     # `$ <cmd>` foreground shell
    ProcessExtension(app, chat_id=MY_CHAT)   # `/process $ <cmd>` background
    TmuxExtension(app, chat_id=MY_CHAT)      # `/tmux $ <cmd>` attachable session
    app.run_polling()

The bundled CLI (`toolbox teleserver`) wires all three onto one app via
:class:`ShellApp` and pulls updates manually with :class:`Poller`.
"""

from .app import ShellApp, main
from .claude import ClaudeExtension
from .job import JobExtension, JobRecord
from .poller import Poller
from .process import ProcessExtension, ProcRecord
from .shell import ShellExtension
from .tmux import TmuxExtension

__all__ = [
    "main",
    "Poller",
    "ShellApp",
    "ShellExtension",
    "JobExtension",
    "JobRecord",
    "ProcessExtension",
    "ProcRecord",
    "TmuxExtension",
    "ClaudeExtension",
]

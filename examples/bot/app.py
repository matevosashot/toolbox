from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from toolbox.configs.telegram import resolve_chat_target
from toolbox.teleserver import TmuxExtension, ClaudeExtension
import os

os.environ["TEST_ENV_VAR"] = "NOT_MUCH_OF_A_SECRET"


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = 278564137
chat_id_2 = -1001382816694

# chat_id = "-1003997668949:1809" #draft
# chat_id_2 = "-1003997668949:1467" #notes

async def print_update(update, context):
    print(update)


async def on_left_button(update, context):
    query = update.callback_query
    # await query.answer()
    await context.bot.send_message(chat_id, f"(LEFT)You pressed: {query.data}")

async def on_right_button(update, context):
    query = update.callback_query
    await context.bot.send_message(chat_id, f"(RIGHT)You pressed: {query.data}")


async def on_startup(app):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Left", callback_data="claude:5bea776a-38d2-45d0-bc02-a3e89f1062ec"),
        InlineKeyboardButton("Right", callback_data="right:4892")]]
    )
    _chat_id, thread_id = resolve_chat_target(chat_id)
    await app.bot.send_message(_chat_id, "Hello, world!", message_thread_id=thread_id, reply_markup=keyboard)


class MyExtension(ClaudeExtension):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        application = self.app
        self._query_data_prefix = "claude"
        
    async def on_button(self,update, context):
        query = update.callback_query
        data = query.data.split(":")
        if len(data) != 2 or data[0] != self._query_data_prefix:
            raise ValueError(f"Invalid query data: {query.data}")
            return
        session_id = data[1]
        command = self._parse_input(f"/claude {session_id}")
        await self._start_job(command, self.chat_id)


def main():
    app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

    extension = MyExtension(app, prefix="", chat_id=chat_id_2, env={"TEST_ENV_VAR": "NOT_MUCH_OF_A_SECRET_2"})

    # app.add_handler(CallbackQueryHandler(on_left_button, pattern="^left"))
    # app.add_handler(CallbackQueryHandler(extension.on_button, pattern=f"^claude"))

    app.add_handler(CallbackQueryHandler(on_right_button, pattern="^right"))
    app.add_handler(MessageHandler(filters.ALL, print_update))
    app.run_polling()


if __name__ == "__main__":
    main()

import os
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

chat_id = "-1003997668949:1809" #draft
chat_id = "-1003997668949:1467" #notes

from telegram import Bot
from toolbox.teleserver.utils import send_message
import asyncio


bot = Bot(TOKEN)

asyncio.run(send_message(bot, chat_id, "Hello, world!"))

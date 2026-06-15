"""Manual `getUpdates` poller, decoupled from any Application instance."""

from __future__ import annotations
import random

from telegram import Bot, Update

import telegram
from telegram import Bot, Update
import os
import time
import asyncio


class Poller:
    """Pull updates from `Bot.get_updates` and advance the offset locally.

    Decoupled from `telegram.ext.Application` so a single poller can feed
    updates into one or more apps in the same process. Each running
    process must own its own poller (the offset cursor is per-poller).
    """

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._offset: int = 0

    async def pull(self, timeout: int = 0) -> list[Update]:
        updates = await self.bot.get_updates(offset=self._offset, timeout=timeout)
        if updates:
            self._offset = max(u.update_id for u in updates) + 1
        return list(updates)



class RandomIntervalPoller:

    def __init__(self, bot: Bot, max_buffer: int = 100, random_sleep: float = 0.1) -> None:
        if max_buffer > telegram.constants.PollingLimit.MAX_LIMIT:
            raise ValueError(f"max_buffer must be less than {telegram.constants.PollingLimit.MAX_LIMIT}")
        
        self.max_buffer = max_buffer
        self.bot = bot
        self._global_offset: int = 0
        self._offset: int = None
        self.random_sleep = random_sleep

    async def pull(self, timeout: int = 0.1) -> list[Update]:
        await asyncio.sleep(random.uniform(0, self.random_sleep))
        updates = await self.bot.get_updates(offset=self._global_offset, timeout=timeout)
        updates = sorted(updates, key=lambda x: x.update_id)

        if len(updates) > 0:
            if len(updates) > self.max_buffer:
                self._global_offset = updates[-self.max_buffer].update_id
            
            if self._offset is None:
                self._offset = updates[-1].update_id + 1

            updates = [u for u in updates if u.update_id >= self._offset] 
            
        if len(updates) > 0:
            self._offset = updates[-1].update_id + 1

        return list(updates)

    async def sleep(self) -> None:
        await asyncio.sleep(self.interval)


async def main(poller):
    while True:
        updates = await poller.pull(timeout=0.1)
        if updates:
            print(len(updates))
            # for update in updates:
            #     print(len(update)
        await poller.sleep(1)

if __name__ == "__main__":
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    bot = Bot(token=TOKEN)
    poller = RandomIntervalPoller(bot, max_buffer=5)

    asyncio.run(main(poller))


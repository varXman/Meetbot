import os
import time

os.environ["TZ"] = "Europe/Kiev"
time.tzset()

import asyncio
import logging

import scheduler
import telegram_bot


async def amain():
    scheduler.rebuild_scheduler()
    await telegram_bot.dp.start_polling(telegram_bot.bot)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(amain())


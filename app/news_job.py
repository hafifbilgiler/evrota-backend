"""CronJob girişi: python -m app.news_job"""
import asyncio
import logging
import sys

from . import cache, news


async def main() -> int:
    await cache.connect()
    try:
        stats = await news.run_once()
        print(stats, flush=True)
        return 0
    except Exception as e:
        logging.getLogger("evrota.news").error("Tarama başarısız: %s", e)
        return 1
    finally:
        await cache.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))

from arq.connections import RedisSettings
from app_src.config import Config
from arq import create_pool
import asyncio
import logging

REDIS_SETTINGS = RedisSettings.from_dsn(Config.REDIS_URL)

async def get_redis_pool():
    max_retries = 3
    for attempt in range(max_retries):
        try:
            redis = await create_pool(REDIS_SETTINGS)
            logging.info(f"✅ Redis connection established (attempt {attempt + 1})")
            try:
                yield redis
            finally:
                await redis.close()
            break
        except Exception as e:
            logging.warning(f"⚠️ Redis connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
                continue
            logging.error(f"❌ Failed to connect to Redis after {max_retries} attempts")
            yield None
            # raise
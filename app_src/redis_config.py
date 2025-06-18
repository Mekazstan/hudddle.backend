from arq.connections import RedisSettings
from app_src.config import Config
from arq import create_pool

REDIS_SETTINGS = RedisSettings.from_dsn(Config.REDIS_URL)

async def get_redis_pool():
    redis = await create_pool(REDIS_SETTINGS)
    try:
        yield redis
    finally:
        await redis.close()
import json
from functools import lru_cache
from typing import Any

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import logger


@lru_cache
def redis_client() -> Redis:
    return Redis.from_url(
        get_settings().redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
    )


async def cache_get(key: str) -> dict[str, Any] | None:
    try:
        value = await redis_client().get(key)
        return json.loads(value) if value else None
    except Exception as exc:
        logger.warning("cache.unavailable", operation="get", error_type=type(exc).__name__)
        return None


async def cache_set(key: str, value: dict[str, Any], ttl: int) -> None:
    try:
        await redis_client().set(key, json.dumps(value), ex=ttl)
    except Exception as exc:
        logger.warning("cache.unavailable", operation="set", error_type=type(exc).__name__)


async def cache_delete(key: str) -> None:
    try:
        await redis_client().delete(key)
    except Exception as exc:
        logger.warning("cache.unavailable", operation="delete", error_type=type(exc).__name__)


async def allow_request(key: str, limit: int, window_seconds: int = 60) -> bool:
    try:
        count = await redis_client().eval(
            "local n=redis.call('INCR',KEYS[1]); "
            "if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            1,
            key,
            window_seconds,
        )
        return count <= limit
    except Exception as exc:
        logger.warning(
            "rate_limit.unavailable",
            operation="increment",
            error_type=type(exc).__name__,
        )
        return True

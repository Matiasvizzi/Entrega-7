"""Cliente Redis asíncrono compartido (redis.asyncio): no bloquea el event loop."""
import redis.asyncio as redis

from app.config import REDIS_URL

_cliente: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _cliente
    if _cliente is None:
        _cliente = redis.from_url(REDIS_URL, decode_responses=True)
    return _cliente


def set_redis(cliente: redis.Redis | None) -> None:
    """Inyecta un cliente (tests con fakeredis)."""
    global _cliente
    _cliente = cliente


async def cerrar_redis() -> None:
    global _cliente
    if _cliente is not None:
        await _cliente.aclose()
        _cliente = None

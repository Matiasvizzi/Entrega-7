"""Estado de los jobs persistido en Redis (hash `job:{id}`).

Estados:  PENDING -> RUNNING -> DONE
                         |-----> AWAITING_APPROVAL -> RUNNING -> DONE | REJECTED
                         +-----> FAILED   (cualquier excepción o timeout)

El estado vive en Redis (no en memoria del proceso): sobrevive a reinicios de la API y
cualquier réplica puede responder el polling del cliente.
"""
import json
import time
import uuid
from typing import Any, Literal

from app.config import JOB_TTL_S
from app.redis_client import get_redis

Estado = Literal["PENDING", "RUNNING", "AWAITING_APPROVAL", "DONE", "REJECTED", "FAILED"]
ESTADOS_FINALES = {"DONE", "REJECTED", "FAILED"}
_JSON_FIELDS = {"result", "approval_request", "approval"}


def _key(job_id: str) -> str:
    return f"job:{job_id}"


def _ahora() -> float:
    return round(time.time(), 3)


async def crear_job(query: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    r = get_redis()
    await r.hset(_key(job_id), mapping={"job_id": job_id, "status": "PENDING", "query": query,
                                         "created_at": _ahora(), "updated_at": _ahora()})
    await r.expire(_key(job_id), JOB_TTL_S)
    return job_id


async def actualizar_job(job_id: str, **campos: Any) -> None:
    mapping = {k: (json.dumps(v, ensure_ascii=False) if k in _JSON_FIELDS else str(v))
               for k, v in campos.items() if v is not None}
    mapping["updated_at"] = str(_ahora())
    await get_redis().hset(_key(job_id), mapping=mapping)


async def leer_job(job_id: str) -> dict[str, Any] | None:
    datos = await get_redis().hgetall(_key(job_id))
    if not datos:
        return None
    for k in _JSON_FIELDS & datos.keys():
        try:
            datos[k] = json.loads(datos[k])
        except ValueError:
            pass
    for k in ("created_at", "updated_at", "started_at", "finished_at"):
        if k in datos:
            datos[k] = float(datos[k])
    if "created_at" in datos and "finished_at" in datos:
        datos["duration_s"] = round(datos["finished_at"] - datos["created_at"], 2)
    return datos


async def listar_job_ids_por_estado(estado: str) -> list[str]:
    r = get_redis()
    ids: list[str] = []
    async for key in r.scan_iter(match="job:*", count=200):
        if await r.hget(key, "status") == estado:
            ids.append(key.removeprefix("job:"))
    return ids

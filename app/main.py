"""API REST (FastAPI) del orquestador multi-agente.

    POST /tasks              encola la tarea y devuelve un job_id (202, no bloquea)
    GET  /tasks/{id}         estado del job (PENDING/RUNNING/AWAITING_APPROVAL/DONE/REJECTED/FAILED)
    POST /tasks/{id}/approve aprobación humana (ver app/hitl.py)
    GET  /health             estado de la API y de Redis

Uso:  uvicorn app.main:app --port 8000
"""
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import hitl
from app.config import JOB_TTL_S, REDIS_URL
from app.graph import construir_grafo
from app.jobs import actualizar_job, crear_job, leer_job, listar_job_ids_por_estado
from app.observability import init_observability
from app.redis_checkpointer import RedisCheckpointSaver
from app.redis_client import cerrar_redis, get_redis
from app.worker import encolar_job


class NuevaTarea(BaseModel):
    query: str = Field(min_length=3, max_length=500, examples=["Museos y parrillas en Recoleta a 2 km"])


def create_app(graph=None, precargar_modelos: bool = True) -> FastAPI:
    """Fábrica de la app. `graph` permite inyectar un grafo de prueba (tests sin LLM)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_observability()
        cliente_ckpt = None
        if graph is not None:
            app.state.graph = graph
        else:
            # Checkpoints de LangGraph en Redis (cliente binario: sin decode_responses)
            cliente_ckpt = aioredis.from_url(REDIS_URL)
            app.state.graph = construir_grafo(RedisCheckpointSaver(cliente_ckpt, ttl_s=JOB_TTL_S))
        if precargar_modelos and graph is None:
            # Carga el modelo de embeddings fuera del event loop (evita un primer request lento)
            from app.tools.analysis import _get_encoder
            await run_in_threadpool(_get_encoder)
        # Jobs que quedaron RUNNING/PENDING de una ejecución anterior ya no tienen worker: FAILED
        for estado in ("RUNNING", "PENDING"):
            for job_id in await listar_job_ids_por_estado(estado):
                await actualizar_job(job_id, status="FAILED", error="Interrumpido por reinicio de la API")
        yield
        if cliente_ckpt is not None:
            await cliente_ckpt.aclose()
        await cerrar_redis()

    app = FastAPI(title="BA Explore AI — API del orquestador multi-agente", version="1.0.0", lifespan=lifespan)
    app.include_router(hitl.router)

    @app.post("/tasks", status_code=202)
    async def crear_tarea(body: NuevaTarea) -> dict[str, Any]:
        """Encola la tarea y responde de inmediato con el job_id; el trabajo corre en segundo plano."""
        job_id = await crear_job(body.query)
        await encolar_job(app, job_id, body.query)
        return {"job_id": job_id, "status": "PENDING", "poll": f"/tasks/{job_id}"}

    @app.get("/tasks/{job_id}")
    async def estado_tarea(job_id: str) -> dict[str, Any]:
        job = await leer_job(job_id)
        if job is None:
            raise HTTPException(404, "job no encontrado")
        if job.get("status") != "FAILED":
            job.pop("traceback", None)
        return job

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"api": "ok", "redis": bool(await get_redis().ping())}

    return app


app = create_app()

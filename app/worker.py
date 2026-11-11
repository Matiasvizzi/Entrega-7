"""Worker: ejecuta los jobs en segundo plano y mantiene su estado en Redis.

Patrón simple pero correcto para esta etapa: cada job es una `asyncio.Task` del mismo proceso
(el grafo usa `ainvoke`, así que nada bloquea el event loop). Un semáforo limita la
concurrencia. Toda excepción —incluido un timeout— se captura y deja el job en FAILED,
para que el cliente nunca quede en un polling infinito.

Para escalar horizontalmente se reemplazaría `lanzar()` por una cola (Arq/Celery) sin tocar
la API ni el grafo: el estado ya está en Redis.
"""
import asyncio
import time
import traceback
from typing import Any

from fastapi import FastAPI
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.config import JOB_TIMEOUT_S, MAX_JOBS_CONCURRENTES, RECURSION_LIMIT
from app.jobs import actualizar_job
from app.observability import contexto_traza

_tareas: set[asyncio.Task] = set()   # referencia fuerte: evita que el GC cancele tareas en vuelo
_semaforo: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _semaforo
    if _semaforo is None:
        _semaforo = asyncio.Semaphore(MAX_JOBS_CONCURRENTES)
    return _semaforo


def _config(job_id: str) -> dict[str, Any]:
    # thread_id = job_id: el checkpointer de Redis guarda el estado del grafo bajo ese hilo
    return {"configurable": {"thread_id": job_id}, "recursion_limit": RECURSION_LIMIT}


def _lanzar(coro) -> None:
    tarea = asyncio.create_task(coro)
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)


async def encolar_job(app: FastAPI, job_id: str, query: str) -> None:
    entrada = {"messages": [HumanMessage(content=query)]}
    _lanzar(_ejecutar(app, job_id, entrada, etiqueta="inicio"))


async def reanudar_job(app: FastAPI, job_id: str, decision: dict[str, Any]) -> None:
    """Reanuda un grafo pausado en el nodo HITL entregándole la decisión humana."""
    await actualizar_job(job_id, status="RUNNING", approval=decision)
    _lanzar(_ejecutar(app, job_id, Command(resume=decision), etiqueta="reanudación"))


async def _ejecutar(app: FastAPI, job_id: str, entrada: Any, etiqueta: str) -> None:
    async with _sem():  # el job queda PENDING mientras espera un cupo
        try:
            await actualizar_job(job_id, status="RUNNING", started_at=time.time())
            with contexto_traza(job_id, fase=etiqueta):
                resultado = await asyncio.wait_for(
                    app.state.graph.ainvoke(entrada, config=_config(job_id)), timeout=JOB_TIMEOUT_S)

            if resultado.get("__interrupt__"):   # pausa HITL: el estado quedó en el checkpointer
                pedido = resultado["__interrupt__"][0].value
                await actualizar_job(job_id, status="AWAITING_APPROVAL", approval_request=pedido)
                return

            aprobacion = resultado.get("aprobacion") or {}
            estado = "REJECTED" if aprobacion and not aprobacion.get("approved") else "DONE"
            await actualizar_job(job_id, status=estado, finished_at=time.time(), result={
                "respuesta": resultado.get("respuesta"),
                "validacion": resultado.get("validacion"),
                "accion_critica": resultado.get("accion_critica") or None,
                "resultado_accion": resultado.get("resultado_accion"),
                "decisiones_supervisor": [{k: d[k] for k in ("paso", "destino", "override")}
                                          for d in resultado.get("decisiones", [])],
                "agentes": [c["agente"] for c in resultado.get("contribuciones", [])],
            })
        except asyncio.TimeoutError:
            await actualizar_job(job_id, status="FAILED", finished_at=time.time(),
                                 error=f"Timeout: el job superó {JOB_TIMEOUT_S}s")
        except asyncio.CancelledError:
            await actualizar_job(job_id, status="FAILED", finished_at=time.time(), error="Job cancelado (apagado de la API)")
            raise
        except Exception as exc:  # ← nunca dejar al cliente esperando: el fallo queda en Redis
            await actualizar_job(job_id, status="FAILED", finished_at=time.time(),
                                 error=f"{type(exc).__name__}: {exc}"[:500],
                                 traceback=traceback.format_exc()[-1500:])

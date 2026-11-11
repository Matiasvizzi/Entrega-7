"""Human-in-the-loop: pausa obligatoria para aprobación humana en acciones críticas.

Una tarea es CRÍTICA cuando implica una acción con efectos secundarios (reservar, enviar un
correo) o un costo muy alto (muchas categorías en un radio máximo). El grafo ejecuta el
trabajo de investigación y análisis normalmente, pero ANTES de ejecutar la acción se detiene
con `interrupt()`: el estado queda guardado en el checkpointer de Redis y el job pasa a
AWAITING_APPROVAL hasta que alguien llame a POST /tasks/{id}/approve.

Las acciones son SIMULADAS (se registran en Redis, no se contacta ningún comercio real).
"""
import re
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.routing import normalizar
from app.state import OrquestadorState

# ------------------------------------------------------------------ detección
_RESERVA = re.compile(r"\breserv\w*")
_ENVIO = re.compile(r"\b(envi\w*|mand\w*)\b.*\b(mail|email|correo)\b")


def detectar_accion_critica(pregunta: str, categorias: list[str], radio_m: int) -> dict[str, Any]:
    """Reglas transparentes (sin LLM) que marcan la tarea como crítica. {} = no crítica."""
    t = normalizar(pregunta)
    if _RESERVA.search(t):
        return {"tipo": "reserva", "motivo": "efecto secundario: crea una reserva a nombre del usuario"}
    if _ENVIO.search(t):
        return {"tipo": "envio_email", "motivo": "efecto secundario: envía un correo con el itinerario"}
    if len(categorias) >= 3 and radio_m >= 4000:
        return {"tipo": "costo_alto", "motivo": "costo alto: 3 categorías en un radio de 4 km o más"}
    return {}


# ----------------------------------------------------------------------- nodos
def _propuesta(state: OrquestadorState) -> dict[str, Any]:
    """Qué se va a ejecutar exactamente (lo que la persona aprueba o rechaza)."""
    ranking = (state.get("analisis") or {}).get("ranking") or {}
    candidatos = ranking.get("gastronomia") or next(iter(ranking.values()), [])
    lugar = candidatos[0] if candidatos else None
    return {"lugar": {"id": lugar["id"], "nombre": lugar["nombre"], "url": lugar["url_fuente"]} if lugar else None,
            "zona": state["ubicacion"]}


def nodo_aprobacion_humana(state: OrquestadorState) -> Command[Literal["ejecutar_accion", "cancelar_accion"]]:
    """Detiene la ejecución hasta recibir una decisión externa (Command(resume=...))."""
    accion = state["accion_critica"]
    decision = interrupt({
        "tipo": accion["tipo"],
        "motivo": accion["motivo"],
        "propuesta": _propuesta(state),
        "pregunta": "¿Aprobás ejecutar esta acción? (POST /tasks/{id}/approve con approved=true|false)",
    })
    aprobado = bool(decision.get("approved")) if isinstance(decision, dict) else False
    return Command(goto="ejecutar_accion" if aprobado else "cancelar_accion",
                   update={"aprobacion": {"approved": aprobado, "comentario": (decision or {}).get("comentario", "")
                                          if isinstance(decision, dict) else ""}})


async def nodo_ejecutar_accion(state: OrquestadorState) -> dict[str, Any]:
    """Ejecuta la acción SIMULADA y deja el registro en Redis (`accion:{id}`)."""
    accion = state["accion_critica"]
    propuesta = _propuesta(state)
    accion_id = uuid.uuid4().hex[:10]
    registro = {"accion_id": accion_id, "tipo": accion["tipo"], "simulada": "true",
                "lugar": (propuesta["lugar"] or {}).get("nombre", "(sin lugar)"), "zona": propuesta["zona"],
                "aprobado_por": "operador", "ejecutada_en": str(time.time())}
    await get_redis().hset(f"accion:{accion_id}", mapping=registro)
    texto = (f"\n\n✅ **Acción ejecutada tras aprobación humana** (simulada): {accion['tipo']} "
             f"en *{registro['lugar']}* — registro `{accion_id}`.")
    return {"respuesta": state["respuesta"] + texto, "resultado_accion": {**registro, "ejecutada": True}}


def nodo_cancelar_accion(state: OrquestadorState) -> dict[str, Any]:
    comentario = (state.get("aprobacion") or {}).get("comentario", "")
    texto = "\n\n🚫 **Acción cancelada:** el operador no aprobó la acción." + (f" Motivo: {comentario}" if comentario else "")
    return {"respuesta": state["respuesta"] + texto, "resultado_accion": {"ejecutada": False}}


def compuerta_critica(state: OrquestadorState) -> Literal["aprobacion_humana", "__end__"]:
    return "aprobacion_humana" if state.get("accion_critica") else "__end__"


# -------------------------------------------------------------------- endpoint
router = APIRouter(tags=["hitl"])


class Aprobacion(BaseModel):
    approved: bool = Field(description="true = ejecutar la acción; false = cancelarla")
    comentario: str = Field(default="", max_length=300)


@router.post("/tasks/{job_id}/approve", status_code=202)
async def aprobar(job_id: str, body: Aprobacion, request: Request) -> dict[str, Any]:
    """Reanuda un job pausado en AWAITING_APPROVAL con la decisión humana."""
    from app.jobs import leer_job
    from app.worker import reanudar_job

    job = await leer_job(job_id)
    if job is None:
        raise HTTPException(404, "job no encontrado")
    if job["status"] != "AWAITING_APPROVAL":
        raise HTTPException(409, f"el job está en estado {job['status']}, no en AWAITING_APPROVAL")
    await reanudar_job(request.app, job_id, {"approved": body.approved, "comentario": body.comentario})
    return {"job_id": job_id, "status": "RUNNING", "approved": body.approved}

"""Pruebas sin LLM ni Redis real (fakeredis): API asíncrona, estado en Redis, FAILED y HITL.

Se usa el grafo REAL del nodo de aprobación humana (app.hitl) sobre un grafo mínimo, con el
checkpointer de Redis propio. Uso:  pytest -q
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis, FakeServer
from langgraph.graph import END, START, StateGraph

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.hitl import (  # noqa: E402
    compuerta_critica,
    detectar_accion_critica,
    nodo_aprobacion_humana,
    nodo_cancelar_accion,
    nodo_ejecutar_accion,
)
from app.main import create_app  # noqa: E402
from app.redis_checkpointer import RedisCheckpointSaver  # noqa: E402
from app.redis_client import set_redis  # noqa: E402
from app.state import OrquestadorState  # noqa: E402

RANKING = {"gastronomia": [{"id": "osm:node:1", "nombre": "Parrilla Test", "url_fuente": "https://osm/1"}]}


def grafo_de_prueba(checkpointer):
    """work -> (si la consulta pide reservar) aprobación humana -> ejecutar | cancelar."""

    def work(state):
        consulta = state["messages"][-1].content
        if "falla" in consulta:
            raise RuntimeError("boom: fallo simulado del agente")
        critica = detectar_accion_critica(consulta, ["gastronomia"], 2000)
        return {"respuesta": "itinerario ok", "accion_critica": critica, "ubicacion": "recoleta",
                "analisis": {"ranking": RANKING}, "categorias": ["gastronomia"]}

    g = StateGraph(OrquestadorState)
    g.add_node("work", work)
    g.add_node("aprobacion_humana", nodo_aprobacion_humana)
    g.add_node("ejecutar_accion", nodo_ejecutar_accion)
    g.add_node("cancelar_accion", nodo_cancelar_accion)
    g.add_edge(START, "work")
    g.add_conditional_edges("work", compuerta_critica, {"aprobacion_humana": "aprobacion_humana", END: END})
    g.add_edge("ejecutar_accion", END)
    g.add_edge("cancelar_accion", END)
    return g.compile(checkpointer=checkpointer)


@pytest_asyncio.fixture
async def cliente():
    server = FakeServer()
    set_redis(FakeAsyncRedis(server=server, decode_responses=True))
    saver = RedisCheckpointSaver(FakeAsyncRedis(server=server))   # binario, como en producción
    app = create_app(graph=grafo_de_prueba(saver), precargar_modelos=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c


async def esperar(c: httpx.AsyncClient, job_id: str, estados: set[str], timeout: float = 10) -> dict:
    for _ in range(int(timeout / 0.05)):
        job = (await c.get(f"/tasks/{job_id}")).json()
        if job["status"] in estados:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"el job no llegó a {estados}: {job}")


def test_deteccion_de_tareas_criticas():
    assert detectar_accion_critica("Reservame una mesa en Palermo", ["gastronomia"], 2000)["tipo"] == "reserva"
    assert detectar_accion_critica("Enviame el itinerario por mail", ["turismo"], 2000)["tipo"] == "envio_email"
    assert detectar_accion_critica("Itinerario completo", ["turismo", "gastronomia", "vida_nocturna"], 5000)["tipo"] == "costo_alto"
    assert detectar_accion_critica("Museos en Recoleta", ["turismo"], 2000) == {}


@pytest.mark.asyncio
async def test_post_encola_y_responde_sin_bloquear(cliente):
    r = await cliente.post("/tasks", json={"query": "museos en recoleta"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "PENDING" and len(body["job_id"]) == 12
    job = await esperar(cliente, body["job_id"], {"DONE"})
    assert job["result"]["respuesta"] == "itinerario ok" and job["duration_s"] >= 0


@pytest.mark.asyncio
async def test_excepcion_en_segundo_plano_deja_el_job_en_failed(cliente):
    job_id = (await cliente.post("/tasks", json={"query": "esto falla"})).json()["job_id"]
    job = await esperar(cliente, job_id, {"FAILED"})
    assert "boom" in job["error"] and "traceback" in job


@pytest.mark.asyncio
async def test_hitl_pausa_y_se_reanuda_al_aprobar(cliente):
    job_id = (await cliente.post("/tasks", json={"query": "reservame una mesa en recoleta"})).json()["job_id"]
    job = await esperar(cliente, job_id, {"AWAITING_APPROVAL"})
    assert job["approval_request"]["tipo"] == "reserva"
    assert job["approval_request"]["propuesta"]["lugar"]["nombre"] == "Parrilla Test"

    r = await cliente.post(f"/tasks/{job_id}/approve", json={"approved": True, "comentario": "dale"})
    assert r.status_code == 202
    job = await esperar(cliente, job_id, {"DONE"})
    assert job["result"]["resultado_accion"]["ejecutada"] is True
    assert "Acción ejecutada tras aprobación humana" in job["result"]["respuesta"]
    # el efecto secundario quedó registrado en Redis
    accion_id = job["result"]["resultado_accion"]["accion_id"]
    from app.redis_client import get_redis
    assert (await get_redis().hget(f"accion:{accion_id}", "simulada")) == "true"


@pytest.mark.asyncio
async def test_hitl_rechazo_cancela_la_accion(cliente):
    job_id = (await cliente.post("/tasks", json={"query": "reservame una mesa"})).json()["job_id"]
    await esperar(cliente, job_id, {"AWAITING_APPROVAL"})
    await cliente.post(f"/tasks/{job_id}/approve", json={"approved": False, "comentario": "no"})
    job = await esperar(cliente, job_id, {"REJECTED"})
    assert job["result"]["resultado_accion"]["ejecutada"] is False
    assert "Acción cancelada" in job["result"]["respuesta"]


@pytest.mark.asyncio
async def test_approve_solo_vale_en_awaiting_approval(cliente):
    job_id = (await cliente.post("/tasks", json={"query": "museos en recoleta"})).json()["job_id"]
    await esperar(cliente, job_id, {"DONE"})
    assert (await cliente.post(f"/tasks/{job_id}/approve", json={"approved": True})).status_code == 409
    assert (await cliente.post("/tasks/inexistente/approve", json={"approved": True})).status_code == 404
    assert (await cliente.get("/tasks/inexistente")).status_code == 404


@pytest.mark.asyncio
async def test_la_pausa_sobrevive_a_un_reinicio(cliente):
    """El interrupt() vive en Redis: un grafo NUEVO (otro proceso) puede reanudar el job."""
    from langgraph.types import Command
    from langchain_core.messages import HumanMessage

    server = FakeServer()
    cfg = {"configurable": {"thread_id": "t-reinicio"}}
    g1 = grafo_de_prueba(RedisCheckpointSaver(FakeAsyncRedis(server=server)))
    r1 = await g1.ainvoke({"messages": [HumanMessage(content="reservame una mesa")]}, config=cfg)
    assert r1["__interrupt__"]

    g2 = grafo_de_prueba(RedisCheckpointSaver(FakeAsyncRedis(server=server)))   # "proceso nuevo"
    set_redis(FakeAsyncRedis(server=server, decode_responses=True))
    r2 = await g2.ainvoke(Command(resume={"approved": True}), config=cfg)
    assert r2["resultado_accion"]["ejecutada"] is True

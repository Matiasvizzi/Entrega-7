"""Grafo: orquestador multi-agente del Módulo 6 + checkpointer de Redis + nodo HITL.

    START -> intake -> supervisor <-> {investigador, analista} -> sintetizador -> validador
                                                                                    |
                                      +------------(tarea crítica)------------------+----(no crítica)--> END
                                      v
                              aprobacion_humana  (interrupt: pausa hasta POST /tasks/{id}/approve)
                                 |            |
                         ejecutar_accion  cancelar_accion --> END
"""
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.analyst_agent import nodo_analista
from app.agents.research_agent import nodo_investigador
from app.agents.supervisor import nodo_supervisor
from app.agents.synthesizer import nodo_sintetizador, nodo_validador
from app.hitl import (
    compuerta_critica,
    detectar_accion_critica,
    nodo_aprobacion_humana,
    nodo_cancelar_accion,
    nodo_ejecutar_accion,
)
from app.routing import detectar_intenciones, extraer_radio, extraer_ubicacion
from app.state import OrquestadorState


def nodo_intake(state: OrquestadorState) -> dict[str, Any]:
    """Router por reglas (sin LLM): categorías, ubicación, radio y si la tarea es crítica."""
    pregunta = state["messages"][-1].content
    categorias = detectar_intenciones(pregunta) or ["turismo"]
    radio = extraer_radio(pregunta)
    return {"categorias": categorias, "ubicacion": extraer_ubicacion(pregunta), "radio_m": radio,
            "pasos": 0, "task_completed": False, "lugares": {}, "contexto_wiki": [], "clima": {},
            "analisis": {}, "centro": {}, "accion_critica": detectar_accion_critica(pregunta, categorias, radio)}


def construir_grafo(checkpointer=None):
    """`checkpointer`: AsyncRedisSaver en producción. Sin checkpointer el interrupt() no puede reanudarse."""
    g = StateGraph(OrquestadorState)
    g.add_node("intake", nodo_intake)
    g.add_node("supervisor", nodo_supervisor)
    g.add_node("investigador", nodo_investigador)
    g.add_node("analista", nodo_analista)
    g.add_node("sintetizador", nodo_sintetizador)
    g.add_node("validador", nodo_validador)
    g.add_node("aprobacion_humana", nodo_aprobacion_humana)   # devuelve Command(goto=Literal[...])
    g.add_node("ejecutar_accion", nodo_ejecutar_accion)
    g.add_node("cancelar_accion", nodo_cancelar_accion)

    g.add_edge(START, "intake")
    g.add_edge("intake", "supervisor")
    g.add_edge("investigador", "supervisor")
    g.add_edge("analista", "supervisor")
    g.add_edge("sintetizador", "validador")
    g.add_conditional_edges("validador", compuerta_critica, {"aprobacion_humana": "aprobacion_humana", END: END})
    g.add_edge("ejecutar_accion", END)
    g.add_edge("cancelar_accion", END)
    return g.compile(checkpointer=checkpointer)

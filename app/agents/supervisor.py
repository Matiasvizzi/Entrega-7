"""Nodo Supervisor: router inteligente y controlador de flujo del grafo.

Decide, turno a turno, quién interviene (investigador | analista) o si la tarea está completa
(FINISH). Usa una RÚBRICA explícita para validar el trabajo de los especialistas antes de
cerrar, y sobre la decisión del LLM aplica REGLAS DE SEGURIDAD deterministas que evitan
el "Supervisor Infinito" y los saltos incoherentes (ej.: analizar sin datos).
"""
import json
from typing import Any, Literal

from langgraph.types import Command
from pydantic import BaseModel, Field

from app.config import MAX_EJECUCIONES_ESPECIALISTA, MAX_PASOS_SUPERVISOR, crear_llm, extraer_texto
from app.state import Destino, OrquestadorState

RUBRICA = """RÚBRICA DE VALIDACIÓN (aplicala en cada decisión, en este orden):
1. COBERTURA: debe haber lugares reales recuperados (con fuente OSM) para CADA categoría pedida.
   Si falta una categoría y el investigador todavía puede intervenir -> investigador, con una
   instrucción concreta sobre qué falta (ej.: "buscá solo gastronomia en Recoleta, radio 2000").
2. ANÁLISIS: el analista debe haber producido un ranking por cada categoría que tenga lugares y un
   recorrido (si hay 2 o más paradas). Si falta algo -> analista, indicando qué falta.
3. CALIDAD: si una categoría quedó vacía incluso tras un reintento, NO insistas: finalizá
   reconociendo esa limitación en vez de bucle de correcciones.
4. SUFICIENCIA: elegí FINISH solo cuando 1 y 2 se cumplen (o cuando 3 aplica). Cada especialista
   puede ejecutarse como máximo 2 veces (una inicial y un refinamiento)."""

SYSTEM_PROMPT = f"""Eres el SUPERVISOR de un orquestador multi-agente de turismo en Buenos Aires.
Equipo:
- investigador: consulta fuentes externas (OpenStreetMap, Wikipedia, Open-Meteo). Recupera datos.
- analista: procesa los datos recuperados (ranking híbrido BM25+embeddings, recorrido, clima).
  No puede trabajar si el investigador todavía no trajo lugares.

Dada la situación actual, decidí quién debe intervenir ahora o si es momento de finalizar.
Escribí en 'instruccion' una orden ESPECÍFICA y breve para el especialista elegido (los especialistas
no ven el historial completo, solo tu instrucción). Con FINISH dejá 'instruccion' vacía.

{RUBRICA}"""


class DecisionSupervisor(BaseModel):
    razonamiento: str = Field(description="Breve evaluación de la rúbrica (1-3 frases)")
    next: Literal["investigador", "analista", "FINISH"]
    instruccion: str = Field(default="", description="Orden específica para el especialista elegido")


def contar_ejecuciones(state: OrquestadorState) -> dict[str, int]:
    cuenta = {"investigador": 0, "analista": 0}
    for c in state.get("contribuciones") or []:
        if c["agente"] in cuenta:
            cuenta[c["agente"]] += 1
    return cuenta


def _siguiente_logico(state: OrquestadorState, ejec: dict[str, int]) -> Destino:
    """Trabajo mínimo que todavía falta, respetando el tope de ejecuciones."""
    lugares = state.get("lugares") or {}
    if not any(lugares.values()) and ejec["investigador"] < MAX_EJECUCIONES_ESPECIALISTA:
        return "investigador"
    if any(lugares.values()) and not (state.get("analisis") or {}).get("ranking") \
            and ejec["analista"] < MAX_EJECUCIONES_ESPECIALISTA:
        return "analista"
    return "FINISH"


def aplicar_reglas_seguridad(propuesta: Destino, state: OrquestadorState, pasos: int) -> tuple[Destino, str]:
    """Corrige la decisión del LLM. Devuelve (destino_final, motivo_del_override o '')."""
    ejec = contar_ejecuciones(state)
    hay_lugares = any((state.get("lugares") or {}).values())

    if pasos >= MAX_PASOS_SUPERVISOR:
        return "FINISH", f"techo de {MAX_PASOS_SUPERVISOR} pasos del supervisor"
    if propuesta == "analista" and not hay_lugares:
        destino = "investigador" if ejec["investigador"] < MAX_EJECUCIONES_ESPECIALISTA else "FINISH"
        return destino, "el analista no puede trabajar sin lugares recuperados"
    if propuesta == "FINISH":
        faltante = _siguiente_logico(state, ejec)
        if faltante != "FINISH":
            return faltante, "no se puede finalizar: falta el trabajo mínimo de un especialista"
    if propuesta in ("investigador", "analista") and ejec[propuesta] >= MAX_EJECUCIONES_ESPECIALISTA:
        return _siguiente_logico(state, ejec), f"{propuesta} alcanzó el máximo de ejecuciones"
    return propuesta, ""


def resumen_para_supervisor(state: OrquestadorState) -> str:
    """Vista compacta del estado: el supervisor no necesita el historial completo."""
    lugares = state.get("lugares") or {}
    analisis = state.get("analisis") or {}
    ejec = contar_ejecuciones(state)
    lineas = [
        f"Consulta del usuario: {extraer_texto(state['messages'][0])}",
        f"Categorías pedidas: {state['categorias']} | Ubicación: {state['ubicacion']} | Radio: {state['radio_m']} m",
        f"Decisión Nº {state['pasos'] + 1} (máximo {MAX_PASOS_SUPERVISOR}).",
        f"Ejecuciones: investigador={ejec['investigador']}, analista={ejec['analista']} (máx {MAX_EJECUCIONES_ESPECIALISTA} c/u).",
        f"Lugares recuperados por categoría: { {c: len(lugares.get(c, [])) for c in state['categorias']} }",
        f"Clima recuperado: {'sí' if state.get('clima') else 'no'} | Wikipedia: {len(state.get('contexto_wiki') or [])} extractos",
        f"Rankings del analista: {list(analisis.get('ranking', {}))} | Recorrido: {'sí' if analisis.get('recorrido') else 'no'}",
    ]
    if state.get("contribuciones"):
        lineas.append("Contribuciones previas:")
        lineas += [f"  - [{c['agente']}] {c['resumen']}" for c in state["contribuciones"]]
    return "\n".join(lineas)


def _instruccion_por_defecto(destino: str, state: OrquestadorState) -> str:
    if destino == "investigador":
        return (f"Recuperá lugares de {', '.join(state['categorias'])} en {state['ubicacion']} "
                f"(radio {state['radio_m']} m); si es un itinerario, incluí también el clima.")
    return "Rankeá los lugares de cada categoría, calculá el recorrido entre las mejores paradas y evaluá el clima si hay datos."


async def nodo_supervisor(state: OrquestadorState) -> Command[Literal["investigador", "analista", "sintetizador"]]:
    pasos = state.get("pasos", 0) + 1
    llm = crear_llm().with_structured_output(DecisionSupervisor)
    try:
        decision: DecisionSupervisor = await llm.ainvoke(
            [("system", SYSTEM_PROMPT), ("human", resumen_para_supervisor(state))])
    except Exception as exc:  # si el LLM falla, el flujo no se rompe: se sigue la lógica mínima
        decision = DecisionSupervisor(razonamiento=f"LLM no disponible ({type(exc).__name__}); lógica mínima",
                                      next=_siguiente_logico(state, contar_ejecuciones(state)))

    destino, motivo = aplicar_reglas_seguridad(decision.next, state, pasos)
    instruccion = decision.instruccion.strip() if destino == decision.next else ""
    if destino != "FINISH" and not instruccion:
        instruccion = _instruccion_por_defecto(destino, state)

    registro: dict[str, Any] = {"paso": pasos, "propuesta_llm": decision.next, "destino": destino,
                                "razonamiento": decision.razonamiento, "instruccion": instruccion,
                                "override": motivo}
    return Command(
        goto="sintetizador" if destino == "FINISH" else destino,
        update={"pasos": pasos, "next_agent": destino, "instruccion": instruccion,
                "task_completed": destino == "FINISH", "decisiones": [registro]},
    )

"""Agente de Investigación: consulta fuentes externas (OpenStreetMap, Wikipedia, Open-Meteo).

Herramientas ACOTADAS: solo lectura de fuentes externas. No rankea ni calcula: eso es del analista.
"""
import json
from typing import Any, Literal

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app.config import (
    MAX_LUGARES_DEVUELTOS,
    RECURSION_LIMIT_ESPECIALISTA,
    crear_llm,
    extraer_texto,
)
from app.state import OrquestadorState
from app.tools.analysis import validar_lugares
from app.tools.geodata import buscar_pois_osm, pronostico_open_meteo, resolver_ubicacion, resumen_wikipedia

SYSTEM_PROMPT = """Eres el AGENTE DE INVESTIGACIÓN de un orquestador turístico de Buenos Aires.
Tu única tarea es RECUPERAR datos de fuentes externas con tus herramientas; no analices ni rankees.

Reglas:
1. Ejecutá buscar_lugares UNA vez por cada categoría pedida en la instrucción.
2. Usá resumen_wikipedia como máximo una vez, solo si la instrucción pide contexto histórico/cultural.
3. Usá pronostico_clima solo si la instrucción pide el clima o un itinerario al aire libre.
4. Si una herramienta devuelve ok=false, reportá el error textual; no inventes datos.
5. Al terminar, respondé en 2-3 líneas: cuántos lugares recuperaste por categoría y qué falló (si algo falló).
No menciones nombres de lugares que no hayan salido de una herramienta."""


@tool
def buscar_lugares(categoria: Literal["turismo", "gastronomia", "vida_nocturna"],
                   ubicacion: str, radio_m: int = 2000) -> str:
    """Busca lugares REALES en OpenStreetMap de una categoría (turismo = museos, galerías,
    monumentos; gastronomia = restaurantes, cafés, parrillas; vida_nocturna = bares, pubs,
    boliches) alrededor de un barrio o hito de Buenos Aires (ej.: 'Recoleta', 'Obelisco').
    El radio va en metros (300 a 5000). Devuelve JSON con los lugares más cercanos, con su
    id y URL de OpenStreetMap, o ok=false con el error. No verifica horarios ni precios."""
    centro = resolver_ubicacion(ubicacion)
    if not centro.get("ok"):
        return json.dumps({"ok": False, "error": centro.get("error", "ubicación no encontrada")}, ensure_ascii=False)
    api = buscar_pois_osm(categoria, centro["lat"], centro["lon"], radio_m)
    if not api.get("ok"):
        return json.dumps({"ok": False, "categoria": categoria, "error": api.get("error")}, ensure_ascii=False)
    lugares = api["lugares"][:MAX_LUGARES_DEVUELTOS]
    return json.dumps({"ok": True, "categoria": categoria, "centro": centro, "total_en_radio": len(api["lugares"]),
                       "lugares": lugares}, ensure_ascii=False)


@tool
def resumen_wikipedia_tool(titulo: str) -> str:
    """Trae el extracto introductorio de un artículo de Wikipedia en español (ej.: 'Museo Nacional
    de Bellas Artes (Argentina)'), con su URL. Útil solo para contexto histórico o cultural.
    Devuelve ok=false si no existe el artículo."""
    return json.dumps(resumen_wikipedia(titulo), ensure_ascii=False)


@tool
def pronostico_clima(lat: float, lon: float) -> str:
    """Obtiene el pronóstico de 3 días (temperatura máxima/mínima y probabilidad máxima de lluvia)
    de Open-Meteo para unas coordenadas. Usar solo si el plan depende del clima."""
    return json.dumps(pronostico_open_meteo(lat, lon), ensure_ascii=False)


HERRAMIENTAS = [buscar_lugares, resumen_wikipedia_tool, pronostico_clima]
_agente = None


def _get_agente():
    global _agente
    if _agente is None:
        _agente = create_agent(model=crear_llm(), tools=HERRAMIENTAS, system_prompt=SYSTEM_PROMPT)
    return _agente


def _json_o_none(texto: str) -> dict[str, Any] | None:
    try:
        d = json.loads(texto)
        return d if isinstance(d, dict) else None
    except (TypeError, ValueError):
        return None


async def nodo_investigador(state: OrquestadorState) -> dict[str, Any]:
    """Ejecuta el agente con SOLO la instrucción del supervisor y el contexto necesario
    (no el historial completo: evita la contaminación de contexto)."""
    entrada = (f"INSTRUCCIÓN DEL SUPERVISOR: {state['instruccion']}\n"
               f"Categorías pedidas: {', '.join(state['categorias'])}\n"
               f"Ubicación: {state['ubicacion']} | Radio: {state['radio_m']} m")
    resultado = await _get_agente().ainvoke({"messages": [HumanMessage(content=entrada)]},
                                            config={"recursion_limit": RECURSION_LIMIT_ESPECIALISTA})
    mensajes = resultado["messages"]

    lugares = {k: list(v) for k, v in (state.get("lugares") or {}).items()}
    wiki = list(state.get("contexto_wiki") or [])
    clima = dict(state.get("clima") or {})
    centro = dict(state.get("centro") or {})
    usadas: list[str] = []
    notas: list[str] = []

    for m in mensajes:
        if not isinstance(m, ToolMessage):
            continue
        usadas.append(m.name)
        datos = _json_o_none(extraer_texto(m))
        if not datos:
            continue
        if not datos.get("ok"):
            notas.append(f"{m.name}: {datos.get('error', 'error')}")
            continue
        if m.name == "buscar_lugares":
            validos, descartados = validar_lugares(datos["lugares"])  # contrato Pydantic
            lugares[datos["categoria"]] = validos
            centro = datos["centro"]
            if descartados:
                notas.append(f"{descartados} registros descartados por esquema")
        elif m.name == "resumen_wikipedia_tool":
            wiki.append({"titulo": datos["titulo"], "extracto": datos["extracto"], "url": datos["url_fuente"]})
        elif m.name == "pronostico_clima":
            clima = datos["pronostico"]

    resumen = extraer_texto(mensajes[-1]).strip()
    conteo = {c: len(v) for c, v in lugares.items()}
    contribucion = {"agente": "investigador", "paso": state.get("pasos", 0), "instruccion": state["instruccion"],
                    "resumen": f"Lugares por categoría: {conteo}. {resumen}"[:600] +
                               (f" Avisos: {notas}" if notas else ""),
                    "herramientas": usadas}
    return {"lugares": lugares, "contexto_wiki": wiki, "clima": clima, "centro": centro,
            "messages": [AIMessage(content=resumen or "Investigación completada.", name="investigador")],
            "contribuciones": [contribucion]}

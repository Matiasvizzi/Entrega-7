"""Agente de Análisis/Cómputo: procesa los datos que recuperó el investigador.

Herramientas disjuntas de las del investigador: no accede a Internet, solo computa sobre el
estado compartido (ranking híbrido BM25 + E5 + RRF, recorrido óptimo aproximado, clima).
Las herramientas se construyen por invocación como closures sobre los lugares del estado,
así el LLM solo pasa categorías/ids (no tiene que copiar listas enormes de datos).
"""
import json
from typing import Any, Literal

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from app.config import RECURSION_LIMIT_ESPECIALISTA, TOP_K_FINAL, crear_llm, extraer_texto
from app.state import OrquestadorState
from app.tools.analysis import evaluar_clima, ranking_hibrido, recorrido_vecino_mas_cercano

SYSTEM_PROMPT = """Eres el AGENTE DE ANÁLISIS de un orquestador turístico de Buenos Aires.
Recibís datos ya recuperados por el investigador y los procesás con tus herramientas.

Reglas:
1. Llamá rankear_lugares UNA vez por cada categoría que tenga lugares disponibles.
2. Con los mejores lugares de todas las categorías, llamá calcular_recorrido UNA vez (ids de las paradas elegidas,
   máximo 1-2 por categoría) para ordenarlos y obtener la distancia total.
3. Si la instrucción menciona el clima o aire libre, llamá evaluar_clima_plan.
4. No inventes lugares ni ids: usá solo los que devuelve rankear_lugares.
5. Al terminar, respondé en 3-4 líneas con las conclusiones numéricas (ranking, km totales, veredicto de clima)."""


def _crear_herramientas(state: OrquestadorState) -> list[BaseTool]:
    lugares_por_cat: dict[str, list[dict]] = state.get("lugares") or {}
    todos = {l["id"]: l for ls in lugares_por_cat.values() for l in ls}
    centro = state.get("centro") or {}
    clima = state.get("clima") or {}

    @tool
    def rankear_lugares(categoria: Literal["turismo", "gastronomia", "vida_nocturna"],
                        consulta: str, top_k: int = TOP_K_FINAL) -> str:
        """Ordena por relevancia los lugares YA recuperados de una categoría respecto de la consulta
        del usuario, combinando búsqueda léxica (BM25) y semántica (embeddings E5) con RRF.
        Devuelve JSON con los top_k lugares (id, nombre, tipo, distancia_km, url). Devuelve
        ok=false si no hay lugares de esa categoría."""
        lugares = lugares_por_cat.get(categoria, [])
        if not lugares:
            return json.dumps({"ok": False, "error": f"No hay lugares recuperados de '{categoria}'"})
        top = ranking_hibrido(consulta, lugares, top_k=max(1, min(int(top_k), 8)))
        compactos = [{k: l.get(k) for k in ("id", "nombre", "tipo", "categoria", "distancia_km", "direccion",
                                           "lat", "lon", "url_fuente", "rrf_score")} for l in top]
        return json.dumps({"ok": True, "categoria": categoria, "top": compactos}, ensure_ascii=False)

    @tool
    def calcular_recorrido(ids: list[str]) -> str:
        """Ordena las paradas indicadas (ids devueltos por rankear_lugares) con el criterio del vecino
        más cercano partiendo del centro de la búsqueda, y suma la distancia total en línea recta (km).
        Devuelve JSON con orden_ids, tramos_km y total_km, o ok=false si algún id no existe."""
        desconocidos = [i for i in ids if i not in todos]
        if desconocidos or not centro:
            return json.dumps({"ok": False, "error": f"ids desconocidos o sin centro: {desconocidos}"})
        res = recorrido_vecino_mas_cercano(centro, [todos[i] for i in ids])
        return json.dumps({"ok": True, **res}, ensure_ascii=False)

    @tool
    def evaluar_clima_plan() -> str:
        """Evalúa el pronóstico ya recuperado (probabilidad de lluvia de hoy y mañana) y dice si el plan
        es apto para el aire libre. Devuelve ok=false si el investigador no trajo el clima."""
        return json.dumps(evaluar_clima(clima), ensure_ascii=False)

    return [rankear_lugares, calcular_recorrido, evaluar_clima_plan]


def _json_o_none(texto: str) -> dict[str, Any] | None:
    try:
        d = json.loads(texto)
        return d if isinstance(d, dict) else None
    except (TypeError, ValueError):
        return None


async def nodo_analista(state: OrquestadorState) -> dict[str, Any]:
    disponibles = {c: len(v) for c, v in (state.get("lugares") or {}).items()}
    pregunta = state["messages"][0].content if state["messages"] else ""
    entrada = (f"INSTRUCCIÓN DEL SUPERVISOR: {state['instruccion']}\n"
               f"Consulta original del usuario: {extraer_texto(state['messages'][0]) if state['messages'] else pregunta}\n"
               f"Lugares disponibles por categoría: {disponibles}\n"
               f"Clima recuperado: {'sí' if state.get('clima') else 'no'}")
    agente = create_agent(model=crear_llm(), tools=_crear_herramientas(state), system_prompt=SYSTEM_PROMPT)
    resultado = await agente.ainvoke({"messages": [HumanMessage(content=entrada)]},
                                     config={"recursion_limit": RECURSION_LIMIT_ESPECIALISTA})
    mensajes = resultado["messages"]

    analisis = {"ranking": dict((state.get("analisis") or {}).get("ranking", {})),
                "recorrido": (state.get("analisis") or {}).get("recorrido"),
                "clima": (state.get("analisis") or {}).get("clima")}
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
        elif m.name == "rankear_lugares":
            analisis["ranking"][datos["categoria"]] = datos["top"]
        elif m.name == "calcular_recorrido":
            analisis["recorrido"] = {k: datos[k] for k in ("orden_ids", "tramos_km", "total_km", "nota")}
        elif m.name == "evaluar_clima_plan":
            analisis["clima"] = {k: v for k, v in datos.items() if k != "ok"}

    resumen = extraer_texto(mensajes[-1]).strip()
    contribucion = {"agente": "analista", "paso": state.get("pasos", 0), "instruccion": state["instruccion"],
                    "resumen": (f"Rankings: {list(analisis['ranking'])}; recorrido: "
                                f"{'sí' if analisis['recorrido'] else 'no'}. {resumen}")[:600] +
                               (f" Avisos: {notas}" if notas else ""),
                    "herramientas": usadas}
    return {"analisis": analisis,
            "messages": [AIMessage(content=resumen or "Análisis completado.", name="analista")],
            "contribuciones": [contribucion]}

"""Cómputo del agente analista: ranking híbrido (BM25 + E5 + RRF), recorrido y clima.

Son funciones puras sobre datos que ya recuperó el investigador: el analista NO accede a
fuentes externas, por eso sus herramientas son disjuntas de las del investigador.
"""
import re
import threading
from functools import lru_cache
from typing import Any

import numpy as np
from pydantic import ValidationError
from rank_bm25 import BM25Okapi

from app.config import MODELO_EMBEDDING, TOP_K_FINAL
from app.routing import normalizar
from app.state import Lugar
from app.tools.geodata import haversine_km

_encoder = None
_encoder_lock = threading.Lock()  # varios jobs concurrentes: el modelo se carga una sola vez


def _get_encoder():
    """Carga diferida y thread-safe del modelo de embeddings."""
    global _encoder
    if _encoder is None:
        with _encoder_lock:
            if _encoder is None:
                from sentence_transformers import SentenceTransformer

                _encoder = SentenceTransformer(MODELO_EMBEDDING, device="cpu")
    return _encoder


@lru_cache(maxsize=2048)
def _tokenizar(texto: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\b\w+\b", normalizar(texto)))


def crear_documento(l: dict) -> str:
    return (f"Nombre: {l['nombre']}. Categoría: {l['categoria']}. Tipo: {l.get('tipo', '')}. "
            f"Dirección: {l.get('direccion', '')}. Detalles: {l.get('descripcion', '')}. "
            f"Distancia en línea recta: {l.get('distancia_km', 0)} km.")


def validar_lugares(crudos: list[dict]) -> tuple[list[dict], int]:
    """Valida con Pydantic y descarta registros mal formados (no inventa valores)."""
    validos, descartados = [], 0
    for item in crudos:
        try:
            validos.append(Lugar.model_validate(item).model_dump())
        except ValidationError:
            descartados += 1
    return validos, descartados


def ranking_hibrido(consulta: str, lugares: list[dict], top_k: int = TOP_K_FINAL) -> list[dict]:
    """BM25 (léxico) + embeddings E5 (semántico), fusionados con Reciprocal Rank Fusion."""
    if not lugares:
        return []
    docs = [crear_documento(l) for l in lugares]

    bm25 = BM25Okapi([list(_tokenizar(d)) or ["vacio"] for d in docs])
    orden_lex = np.argsort(-np.asarray(bm25.get_scores(list(_tokenizar(consulta)))))

    enc = _get_encoder()  # E5 exige los prefijos "passage: " / "query: "
    emb_d = enc.encode(["passage: " + d for d in docs], normalize_embeddings=True, show_progress_bar=False)
    emb_q = enc.encode(["query: " + consulta], normalize_embeddings=True, show_progress_bar=False)
    orden_den = np.argsort(-(emb_d @ emb_q[0]))

    rrf = np.zeros(len(lugares))
    for ranking in (orden_lex, orden_den):
        for pos, idx in enumerate(ranking, start=1):
            rrf[int(idx)] += 1.0 / (60 + pos)
    mejores = np.argsort(-rrf)[:top_k]
    return [{**lugares[int(i)], "rrf_score": round(float(rrf[i]), 5)} for i in mejores]


def recorrido_vecino_mas_cercano(centro: dict, lugares: list[dict]) -> dict[str, Any]:
    """Ordena las paradas por vecino más cercano desde el centro y suma la distancia en línea recta."""
    pendientes = list(lugares)
    lat, lon = centro["lat"], centro["lon"]
    orden, tramos, total = [], [], 0.0
    while pendientes:
        sig = min(pendientes, key=lambda p: haversine_km(lat, lon, p["lat"], p["lon"]))
        d = haversine_km(lat, lon, sig["lat"], sig["lon"])
        tramos.append(d)
        total += d
        orden.append(sig["id"])
        lat, lon = sig["lat"], sig["lon"]
        pendientes.remove(sig)
    return {"orden_ids": orden, "tramos_km": tramos, "total_km": round(total, 2),
            "nota": "Distancias geodésicas en línea recta, no tiempo de viaje."}


def evaluar_clima(pronostico: dict) -> dict[str, Any]:
    """Regla simple: probabilidad de lluvia máxima de hoy y mañana -> apto para aire libre."""
    try:
        prob = pronostico["precipitation_probability_max"][:2]
        tmax = pronostico["temperature_2m_max"][:2]
        tmin = pronostico["temperature_2m_min"][:2]
    except (KeyError, TypeError):
        return {"ok": False, "error": "Sin pronóstico disponible"}
    peor = max(p for p in prob if p is not None) if any(p is not None for p in prob) else None
    if peor is None:
        return {"ok": False, "error": "Sin probabilidad de lluvia"}
    veredicto = "apto para actividades al aire libre" if peor < 40 else "conviene priorizar lugares techados"
    return {"ok": True, "prob_lluvia_max_pct": peor, "tmax": tmax, "tmin": tmin, "veredicto": veredicto}

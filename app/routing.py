"""Router de intención SIN LLM (reglas transparentes), tomado del notebook BA Explore AI.

Extrae categorías, ubicación y radio de la consulta. El supervisor (LLM) decide después
QUIÉN trabaja y CUÁNDO; este módulo solo fija los hechos de la consulta.
"""
import re
import unicodedata

from app.config import RADIO_POR_DEFECTO_M

PALABRAS_CLAVE: dict[str, list[str]] = {
    "turismo": ["museo", "museos", "monumento", "monumentos", "historia", "historicos", "historico",
                "plaza", "plazas", "atraccion", "visitar", "pasear", "galeria", "arte", "turistico",
                "turismo", "sitios"],
    "gastronomia": ["restaurante", "restaurantes", "parrilla", "parrillas", "comer", "cenar", "almorzar",
                    "gastronomia", "pizza", "empanadas", "cafeteria", "cafe", "cafes", "comida"],
    "vida_nocturna": ["bares", "bar", "pub", "pubs", "boliche", "boliches", "discoteca", "discotecas",
                      "tragos", "cocteles", "cocktails", "salir de noche", "vida nocturna"],
}
CATEGORIAS = list(PALABRAS_CLAVE)

BARRIOS = ["obelisco", "plaza de mayo", "recoleta", "palermo", "san telmo", "puerto madero",
           "teatro colon", "caminito", "barracas", "caballito", "belgrano"]


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes."""
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def detectar_intenciones(pregunta: str) -> list[str]:
    texto = normalizar(pregunta)
    if any(x in texto for x in ["itinerario completo", "dia completo", "plan de un dia", "planificar un dia"]):
        return CATEGORIAS.copy()
    encontrados = [c for c, palabras in PALABRAS_CLAVE.items()
                   if any(re.search(r"\b" + re.escape(p) + r"\b", texto) for p in palabras)]
    return encontrados


def extraer_ubicacion(pregunta: str) -> str:
    t = normalizar(pregunta)
    for nombre in sorted(BARRIOS, key=len, reverse=True):
        if re.search(r"\b" + re.escape(nombre) + r"\b", t):
            return nombre
    m = re.search(r"\bcerca\s+de(?:l)?\s+([^,.;!?]+?)(?:\s+y\s+|\s+para\s+|$)", t)
    if not m:
        return "obelisco"  # centro didáctico por defecto
    ref = re.sub(r"\s+(?:a menos de|hasta|a)\s+\d+(?:[.,]\d+)?\s*(?:km|kilometros?|metros?|m)\b.*$", "",
                 m.group(1).strip()).strip()
    return ref or "obelisco"


def extraer_radio(pregunta: str) -> int:
    t = normalizar(pregunta)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(km|kilometros?|metros?|m)\b", t)
    if not m:
        return RADIO_POR_DEFECTO_M
    n = float(m.group(1).replace(",", "."))
    metros = n * 1000 if m.group(2).startswith("k") else n
    return int(min(5000, max(300, metros)))

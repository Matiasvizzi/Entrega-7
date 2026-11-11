"""Acceso a APIs públicas gratuitas: OpenStreetMap (Overpass/Nominatim), Wikipedia y Open-Meteo.

Es la capa de datos del agente investigador. En el notebook original estas funciones vivían
en un servidor MCP (stdio); aquí son funciones Python directas, con la misma lógica y caché.

Atribución: © colaboradores de OpenStreetMap (ODbL); Wikipedia (Wikimedia);
Open-Meteo (CC BY 4.0, uso no comercial). Nominatim: máx. 1 solicitud/segundo.
"""
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

from app.config import CACHE_DIR, USER_AGENT
from app.routing import normalizar

HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

ANCLAS: dict[str, tuple[float, float, str]] = {
    "obelisco": (-34.60372, -58.38159, "Obelisco"),
    "plaza de mayo": (-34.60815, -58.37039, "Plaza de Mayo"),
    "recoleta": (-34.58830, -58.39250, "Recoleta"),
    "palermo": (-34.58890, -58.43060, "Palermo"),
    "san telmo": (-34.62120, -58.37350, "San Telmo"),
    "puerto madero": (-34.60980, -58.36350, "Puerto Madero"),
    "teatro colon": (-34.60110, -58.38330, "Teatro Colón"),
    "caminito": (-34.63830, -58.36280, "Caminito"),
    "caballito": (-34.62060, -58.44270, "Caballito"),
    "belgrano": (-34.56240, -58.45660, "Belgrano"),
    "barracas": (-34.64360, -58.38370, "Barracas"),
}

FILTROS_OSM = {
    "turismo": ['["tourism"~"^(museum|art_gallery|attraction|viewpoint)$"]',
                '["historic"~"^(monument|memorial|archaeological_site|castle)$"]'],
    "gastronomia": ['["amenity"~"^(restaurant|cafe|fast_food|food_court)$"]'],
    "vida_nocturna": ['["amenity"~"^(bar|pub|nightclub)$"]'],
}


# ----------------------------------------------------------------- utilidades
def _cache_path(prefijo: str, payload: Any):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    return CACHE_DIR / f"{prefijo}_{digest}.json"


def _leer_cache(path, ttl_horas: float) -> dict | None:
    if path.exists() and time.time() - path.stat().st_mtime < ttl_horas * 3600:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def _guardar_cache(path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia geodésica en línea recta (no es tiempo de viaje)."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(min(1, math.sqrt(h))), 3)


# ------------------------------------------------------------------ ubicación
def resolver_ubicacion(nombre: str) -> dict[str, Any]:
    """Coordenadas de un barrio/hito: ancla local o Nominatim (cacheado)."""
    nombre = nombre.strip()
    if not nombre or len(nombre) > 110:
        return {"ok": False, "error": "El nombre debe tener entre 1 y 110 caracteres"}
    norm = normalizar(nombre)
    for clave, (lat, lon, etiqueta) in ANCLAS.items():
        if norm == clave or (clave in norm and len(norm) <= len(clave) + 20):
            return {"ok": True, "nombre": etiqueta, "lat": lat, "lon": lon}

    path = _cache_path("nominatim", {"q": nombre})
    cached = _leer_cache(path, 24 * 30)
    if cached:
        return cached
    pacing = CACHE_DIR / "nominatim_last_call.txt"
    try:
        ultimo = float(pacing.read_text()) if pacing.exists() else 0.0
    except (OSError, ValueError):
        ultimo = 0.0
    time.sleep(max(0.0, 1.1 - (time.time() - ultimo)))  # política: 1 req/s
    try:
        pacing.write_text(str(time.time()))
        r = requests.get("https://nominatim.openstreetmap.org/search", headers=HEADERS, timeout=20, params={
            "q": f"{nombre}, Ciudad Autónoma de Buenos Aires, Argentina", "format": "jsonv2",
            "limit": 1, "countrycodes": "ar"})
        r.raise_for_status()
        data = r.json()
        if not data:
            return {"ok": False, "error": f"No se pudo ubicar: {nombre}"}
        res = {"ok": True, "nombre": data[0].get("display_name", nombre),
               "lat": float(data[0]["lat"]), "lon": float(data[0]["lon"])}
        _guardar_cache(path, res)
        return res
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        return {"ok": False, "error": f"Error geocodificando {nombre}: {type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- OpenStreetMap
def _poi(elemento: dict, categoria: str, lat: float, lon: float) -> dict | None:
    tags = elemento.get("tags", {})
    nombre = tags.get("name") or tags.get("name:es")
    coord = elemento.get("center", elemento)
    if not nombre or "lat" not in coord or "lon" not in coord:
        return None
    plat, plon = float(coord["lat"]), float(coord["lon"])
    direccion = " ".join(x for x in (tags.get("addr:street", ""), tags.get("addr:housenumber", "")) if x)
    descripcion = "; ".join(f"{k}: {tags[k]}" for k in (
        "description", "cuisine", "tourism", "historic", "amenity", "wheelchair", "operator", "brand")
        if tags.get(k))
    tipo = next((str(tags[k]) for k in ("tourism", "historic", "amenity") if tags.get(k)), "sin_clasificar")
    return {
        "id": f"osm:{elemento['type']}:{elemento['id']}", "nombre": str(nombre), "categoria": categoria,
        "tipo": tipo, "direccion": direccion, "lat": plat, "lon": plon,
        "distancia_km": haversine_km(lat, lon, plat, plon), "descripcion": descripcion,
        "wikipedia": tags.get("wikipedia", ""),
        "url_fuente": f"https://www.openstreetmap.org/{elemento['type']}/{elemento['id']}",
        "fuente": "OpenStreetMap/Overpass",
    }


def buscar_pois_osm(categoria: str, lat: float, lon: float, radio_m: int = 2000) -> dict[str, Any]:
    """Puntos de interés reales de OSM dentro del radio, ordenados por cercanía y sin duplicados."""
    if categoria not in FILTROS_OSM:
        return {"ok": False, "error": "Categoría inválida", "lugares": []}
    if not (-34.9 <= lat <= -34.3 and -58.8 <= lon <= -58.1):
        return {"ok": False, "error": "La demostración está restringida al AMBA", "lugares": []}
    radio_m = min(5000, max(300, int(radio_m)))

    path = _cache_path("overpass", {"c": categoria, "lat": round(lat, 5), "lon": round(lon, 5), "r": radio_m})
    cached = _leer_cache(path, 24)
    if cached:
        cached["cache_hit"] = True
        return cached

    bloques = "\n".join(f"  nwr(around:{radio_m},{lat:.6f},{lon:.6f}){f};" for f in FILTROS_OSM[categoria])
    query = f"[out:json][timeout:45];\n(\n{bloques}\n);\nout center;"
    errores: list[str] = []
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(endpoint, data={"data": query}, headers=HEADERS, timeout=65)
            r.raise_for_status()
            candidatos = [p for obj in r.json().get("elements", [])
                          if (p := _poi(obj, categoria, lat, lon)) and p["distancia_km"] * 1000 <= radio_m + 1]
            vistos, unicos = set(), []
            for lugar in sorted(candidatos, key=lambda z: z["distancia_km"]):
                clave = (lugar["nombre"].casefold(), round(lugar["lat"], 4), round(lugar["lon"], 4))
                if clave not in vistos:
                    vistos.add(clave)
                    unicos.append(lugar)
            res = {"ok": True, "categoria": categoria, "lugares": unicos, "cache_hit": False,
                   "consultado_utc": datetime.now(timezone.utc).isoformat(), "endpoint": endpoint}
            _guardar_cache(path, res)
            return res
        except (requests.RequestException, ValueError, KeyError) as exc:
            errores.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    return {"ok": False, "lugares": [], "error": " | ".join(errores)}


# -------------------------------------------------------- Wikipedia / Open-Meteo
def resumen_wikipedia(titulo: str) -> dict[str, Any]:
    titulo = titulo.strip().removeprefix("es:")
    if not titulo or len(titulo) > 140:
        return {"ok": False, "error": "Título inválido"}
    path = _cache_path("wikipedia", {"t": titulo})
    cached = _leer_cache(path, 24 * 14)
    if cached:
        return cached
    try:
        r = requests.get("https://es.wikipedia.org/w/api.php", headers=HEADERS, timeout=22, params={
            "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
            "titles": titulo, "format": "json", "formatversion": 2})
        r.raise_for_status()
        pagina = r.json()["query"]["pages"][0]
        if "missing" in pagina:
            return {"ok": False, "error": f"No hay artículo: {titulo}"}
        title = pagina.get("title", titulo)
        res = {"ok": True, "titulo": title, "extracto": pagina.get("extract", "")[:900],
               "url_fuente": "https://es.wikipedia.org/wiki/" + requests.utils.quote(title.replace(" ", "_"), safe="")}
        _guardar_cache(path, res)
        return res
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        return {"ok": False, "error": f"Wikipedia no disponible: {exc}"}


def pronostico_open_meteo(lat: float, lon: float) -> dict[str, Any]:
    path = _cache_path("meteo", {"lat": round(lat, 3), "lon": round(lon, 3)})
    cached = _leer_cache(path, 2)
    if cached:
        return cached
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", headers=HEADERS, timeout=22, params={
            "latitude": lat, "longitude": lon, "timezone": "America/Argentina/Buenos_Aires",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max", "forecast_days": 3})
        r.raise_for_status()
        res = {"ok": True, "pronostico": r.json().get("daily", {}), "fuente": "Open-Meteo (CC BY 4.0)"}
        _guardar_cache(path, res)
        return res
    except (requests.RequestException, ValueError, KeyError) as exc:
        return {"ok": False, "error": f"Clima no disponible: {exc}"}

"""Estado compartido del orquestador y contratos de datos.

El estado hereda de MessagesState (reducer `add_messages`) y agrega campos con DUEÑO
claro, para rastrear qué agente aportó qué sin depender del historial de mensajes:

| Campo                 | Escribe               | Lee                        |
|-----------------------|-----------------------|----------------------------|
| categorias/ubicacion/radio_m | intake         | todos                      |
| lugares, centro, contexto_wiki, clima | investigador | analista, sintetizador |
| analisis              | analista              | supervisor, sintetizador   |
| next_agent, instruccion, pasos, task_completed | supervisor | grafo       |
| contribuciones, decisiones | especialistas / supervisor (acumulan) | todos |
| respuesta, validacion | sintetizador / validador | usuario                 |
"""
import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

Categoria = Literal["turismo", "gastronomia", "vida_nocturna"]
Destino = Literal["investigador", "analista", "FINISH"]


class Lugar(BaseModel):
    """Contrato de un lugar real (OpenStreetMap). No admite precios, horarios ni
    calificaciones que no existan en los datos."""

    id: str = Field(min_length=3)
    nombre: str = Field(min_length=1)
    categoria: str
    tipo: str = ""
    direccion: str = ""
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    distancia_km: float = Field(ge=0)
    descripcion: str = ""
    url_fuente: str = ""
    fuente: str = "OpenStreetMap/Overpass"


class Contribucion(TypedDict):
    """Registro de qué aportó cada agente en cada turno."""

    agente: str
    paso: int
    instruccion: str
    resumen: str
    herramientas: list[str]


class OrquestadorState(MessagesState):
    # --- intake (router por reglas) ---
    categorias: list[str]
    ubicacion: str
    radio_m: int
    # --- control del supervisor ---
    next_agent: Destino
    instruccion: str          # instrucción específica para el próximo especialista
    pasos: int                # decisiones del supervisor hasta ahora
    task_completed: bool
    # --- datos aportados por los especialistas ---
    centro: dict              # {"lat", "lon", "nombre"}
    lugares: dict[str, list[dict]]   # categoría -> lugares validados (investigador)
    contexto_wiki: list[dict]
    clima: dict
    analisis: dict            # {"ranking": {cat: [...]}, "recorrido": {...}, "clima": {...}}
    # --- trazabilidad (acumulan con operator.add) ---
    contribuciones: Annotated[list[Contribucion], operator.add]
    decisiones: Annotated[list[dict], operator.add]
    # --- salida ---
    respuesta: str
    validacion: dict
    # --- human-in-the-loop (Entrega 7) ---
    accion_critica: dict      # {} si la tarea no es crítica; {"tipo", "motivo"} si requiere aprobación
    aprobacion: dict          # decisión humana: {"approved": bool, "comentario": str}
    resultado_accion: dict    # resultado de ejecutar (o cancelar) la acción

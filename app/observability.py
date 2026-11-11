"""Observabilidad: trazas de cada ejecución del grafo hacia Arize Phoenix (OpenInference).

Con `auto_instrument=True`, OpenInference instrumenta LangChain/LangGraph: cada nodo del grafo,
cada llamada al LLM (con tokens de entrada/salida, de donde Phoenix calcula el costo) y cada
herramienta se registran como spans, agrupados por sesión = job_id.

Alternativa LangSmith: definir LANGSMITH_TRACING=true y LANGSMITH_API_KEY en el .env; LangChain
lo detecta solo y no hace falta código adicional.
"""
import os
from contextlib import contextmanager
from typing import Any, Iterator

PHOENIX_PROJECT = os.getenv("PHOENIX_PROJECT_NAME", "ba-explore-api")
PHOENIX_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces")

_activa = False


def init_observability() -> bool:
    """Registra el colector de Phoenix. Si no está disponible, la API sigue funcionando sin trazas."""
    global _activa
    if os.getenv("OBSERVABILIDAD", "phoenix").lower() in ("off", "none", "0", "false"):
        print("ℹ️  Observabilidad desactivada (OBSERVABILIDAD=off)")
        return False
    try:
        from phoenix.otel import register

        register(project_name=PHOENIX_PROJECT, endpoint=PHOENIX_ENDPOINT, auto_instrument=True, batch=True)
        _activa = True
        print(f"🔭 Trazas -> Phoenix ({PHOENIX_ENDPOINT}) | proyecto '{PHOENIX_PROJECT}'")
    except Exception as exc:  # observabilidad nunca debe tumbar la API
        print(f"⚠️  No se pudo iniciar Phoenix ({type(exc).__name__}: {exc}); la API sigue sin trazas")
    return _activa


@contextmanager
def contexto_traza(job_id: str, **metadata: Any) -> Iterator[None]:
    """Etiqueta todos los spans del bloque con session.id = job_id (y metadata), para filtrar
    en Phoenix las trazas de una ejecución concreta."""
    if not _activa:
        yield
        return
    from openinference.instrumentation import using_attributes

    with using_attributes(session_id=job_id, metadata=metadata or None):
        yield

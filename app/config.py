"""Configuración común: variables de entorno, LLM y límites del orquestador."""
import os
from pathlib import Path

from dotenv import load_dotenv

# En redes corporativas con inspección TLS, usar el almacén de certificados del
# sistema evita errores CERTIFICATE_VERIFY_FAILED. Es opcional.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent  # raíz del proyecto (.env, ba_cache)
load_dotenv(BASE_DIR / ".env")

CACHE_DIR = BASE_DIR / "ba_cache"
CACHE_DIR.mkdir(exist_ok=True)

USER_AGENT = os.getenv(
    "BA_USER_AGENT", "BAExploreAI-Docencia/1.0 (demostracion individual; identificador BAExploreAI)"
)

# --- Límites del orquestador (anti "Supervisor Infinito") ---
MAX_PASOS_SUPERVISOR = 6        # techo duro de decisiones del supervisor
MAX_EJECUCIONES_ESPECIALISTA = 2  # 1 ejecución inicial + 1 refinamiento
RECURSION_LIMIT = 40            # techo de super-steps de LangGraph
RECURSION_LIMIT_ESPECIALISTA = 14

# --- Datos ---
RADIO_POR_DEFECTO_M = 2000
MAX_LUGARES_DEVUELTOS = 25      # por búsqueda, para no inflar el contexto del LLM
TOP_K_FINAL = 5

MODELO_EMBEDDING = "intfloat/multilingual-e5-small"


def crear_llm():
    """LLM de los agentes (Gemini por defecto; configurable con GEMINI_MODEL)."""
    if not os.getenv("GOOGLE_API_KEY"):
        raise SystemExit("❌ Falta GOOGLE_API_KEY. Copiá .env.example a .env y completalo.")
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"),
        temperature=0,
        max_retries=6,  # el modelo a veces responde 503 por alta demanda
    )


def extraer_texto(mensaje) -> str:
    """Normaliza el content (Gemini a veces devuelve una lista de bloques)."""
    contenido = mensaje.content
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        return "".join(b.get("text", "") for b in contenido if isinstance(b, dict))
    return str(contenido)


# --- API / Redis / observabilidad (Entrega 7) ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MAX_JOBS_CONCURRENTES = int(os.getenv("MAX_JOBS_CONCURRENTES", "5"))
JOB_TIMEOUT_S = int(os.getenv("JOB_TIMEOUT_S", "420"))   # un job colgado pasa a FAILED, no queda en polling infinito
JOB_TTL_S = int(os.getenv("JOB_TTL_S", str(7 * 24 * 3600)))
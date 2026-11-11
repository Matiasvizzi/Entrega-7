# API de producción y monitoreo activo — Pre-entrega 7

API REST (**FastAPI**) que expone el orquestador multi-agente de la Entrega 6 (BA Explore AI) con:

- **Endpoints asíncronos:** `POST /tasks` encola el trabajo y devuelve un `job_id` al instante.
- **Redis:** estado de los jobs y checkpoints de LangGraph.
- **Observabilidad:** trazas de cada nodo, tokens, costo y latencia en **Arize Phoenix** (OpenInference).
- **Human-in-the-loop:** pausa obligatoria con aprobación humana en tareas críticas.

## Arquitectura

```
cliente ──POST /tasks──► FastAPI ──► crea job (Redis: PENDING) ──► asyncio.Task (worker) ──► grafo LangGraph
   ▲                        │ 202 + job_id                              │                        │
   │                        │                                           ▼                        ▼
   └──GET /tasks/{id}──► lee estado en Redis ◄──── RUNNING/DONE/FAILED/AWAITING_APPROVAL   checkpoints en Redis
                                                                                              │
                                         trazas OpenTelemetry/OpenInference ──────────────► Phoenix
```

| Archivo | Rol |
|---|---|
| `app/main.py` | FastAPI: `POST /tasks`, `GET /tasks/{id}`, `GET /health`. Ciclo de vida (checkpointer, precarga de modelos). |
| `app/worker.py` | Ejecuta el job en segundo plano y actualiza el estado en Redis. Captura **toda** excepción → `FAILED`. |
| `app/jobs.py` | Estado de los jobs en Redis (hash `job:{id}`). |
| `app/graph.py` | Orquestador del Módulo 6 + checkpointer de Redis + rama HITL. |
| `app/hitl.py` | Nodo de aprobación humana (`interrupt`) y endpoint `POST /tasks/{id}/approve`. |
| `app/redis_checkpointer.py` | Checkpointer de LangGraph sobre Redis (ver nota más abajo). |
| `app/observability.py` | Inicialización de Phoenix y etiquetado de trazas por `job_id`. |
| `app/agents/`, `app/tools/` | Supervisor, investigador, analista, sintetizador y herramientas (Entrega 6). |
| `scripts/load_test.py` | Prueba de carga: 5 peticiones concurrentes, calcula p50/p95. |
| `scripts/capturar_dashboard.py` | Capturas automáticas del dashboard de Phoenix. |
| `tests/` | 7 pruebas sin LLM ni Redis real (fakeredis). |

### Grafo

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	intake(intake)
	supervisor(supervisor)
	investigador(investigador)
	analista(analista)
	sintetizador(sintetizador)
	validador(validador)
	aprobacion_humana(aprobacion_humana)
	ejecutar_accion(ejecutar_accion)
	cancelar_accion(cancelar_accion)
	__end__([<p>__end__</p>]):::last
	__start__ --> intake;
	analista --> supervisor;
	aprobacion_humana -.-> cancelar_accion;
	aprobacion_humana -.-> ejecutar_accion;
	intake --> supervisor;
	investigador --> supervisor;
	sintetizador --> validador;
	supervisor -.-> analista;
	supervisor -.-> investigador;
	supervisor -.-> sintetizador;
	validador -.-> __end__;
	validador -.-> aprobacion_humana;
	cancelar_accion --> __end__;
	ejecutar_accion --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## Ciclo de vida de un job

```
PENDING → RUNNING → DONE
                 ├→ AWAITING_APPROVAL → RUNNING → DONE | REJECTED     (tareas críticas)
                 └→ FAILED                                            (excepción o timeout)
```

- **No bloquea el event loop:** el endpoint solo escribe en Redis y lanza una `asyncio.Task`; el grafo corre con `ainvoke`, las herramientas síncronas (requests, embeddings) las ejecuta LangGraph en un thread pool, y Redis se usa con `redis.asyncio`.
- **Errores en segundo plano:** el worker envuelve la ejecución en `try/except`; ante cualquier excepción o timeout (`JOB_TIMEOUT_S`) el job pasa a `FAILED` con `error` y `traceback` en Redis, para que el cliente nunca quede esperando en un polling infinito. Al arrancar, los jobs que quedaron `RUNNING`/`PENDING` de una ejecución anterior se marcan `FAILED`.
- **Concurrencia acotada:** un semáforo (`MAX_JOBS_CONCURRENTES`, 5 por defecto) limita los jobs simultáneos.

## Human-in-the-loop

Una tarea es **crítica** (`app/hitl.py::detectar_accion_critica`, reglas sin LLM) cuando implica:

| Tipo | Disparador | Motivo |
|---|---|---|
| `reserva` | "reservá…" | efecto secundario: crea una reserva |
| `envio_email` | "enviame… por mail" | efecto secundario: envía un correo |
| `costo_alto` | 3 categorías en radio ≥ 4 km | muchas llamadas a APIs y al LLM |

El grafo hace la investigación y el análisis, y **antes** de ejecutar la acción llama a `interrupt()`: el estado queda guardado en Redis y el job pasa a `AWAITING_APPROVAL`, con la propuesta concreta (qué lugar, qué acción). Hasta que alguien llame a `POST /tasks/{id}/approve` no se ejecuta nada. Las acciones son **simuladas**: se registran en Redis (`accion:{id}`), no se contacta ningún comercio.

```bash
curl -X POST localhost:8000/tasks -H "Content-Type: application/json" \
     -d '{"query": "Reservame una mesa en una parrilla de Recoleta"}'        # → {"job_id": "…", "status": "PENDING"}
curl localhost:8000/tasks/<job_id>                                             # → AWAITING_APPROVAL + approval_request
curl -X POST localhost:8000/tasks/<job_id>/approve -H "Content-Type: application/json" \
     -d '{"approved": true, "comentario": "ok"}'                              # → reanuda; luego DONE (o REJECTED si approved=false)
```

Como el checkpoint está en Redis y no en memoria, la pausa **sobrevive a un reinicio de la API** (cubierto por `test_la_pausa_sobrevive_a_un_reinicio`).

### Nota sobre el checkpointer de Redis

El paquete oficial `langgraph-checkpoint-redis` exige **Redis Stack** (módulos RedisJSON y RediSearch). Para que la API funcione con cualquier Redis (`redis:7`, Redis administrado, Redis local) implementé `RedisCheckpointSaver` sobre hashes y sorted sets. Implementa la interfaz asíncrona de `BaseCheckpointSaver` (checkpoints, escrituras pendientes —donde queda el `interrupt`—, listado y borrado). No tiene las optimizaciones del oficial (por ejemplo, guarda el checkpoint completo en cada paso).

## Cómo levantarlo

Requiere **Python 3.12+**, un Redis y una `GOOGLE_API_KEY` gratuita ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)).

### Opción A — Docker Compose (Redis + Phoenix + API)

```bash
copy .env.example .env      # completar GOOGLE_API_KEY
docker compose up --build
# API:     http://localhost:8000/docs      Phoenix: http://localhost:6006
```

### Opción B — Local

```bash
python -m venv .venv && .venv\Scripts\activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                              # completar GOOGLE_API_KEY

# 1. Redis (cualquiera de estas)
docker run -d -p 6379:6379 redis:7-alpine           # o un Redis local / Memurai en Windows

# 2. Phoenix (entorno aparte para no mezclar dependencias)
python -m venv .venv-phoenix && .venv-phoenix\Scripts\pip install arize-phoenix
.venv-phoenix\Scripts\python -m phoenix.server.main serve      # http://localhost:6006

# 3. API
uvicorn app.main:app --port 8000
```

Para correr los tests (no requieren Redis ni LLM): `pip install -r requirements-dev.txt && pytest -q`.

## Prueba de carga: 5 peticiones concurrentes

```bash
python scripts/load_test.py --url http://localhost:8000 --concurrencia 5
```

Lanza 5 consultas a la vez, sondea cada job hasta que termina y escribe `load_test_resultado.json` con la latencia de cada uno (p50, p95, máximo). Luego, en Phoenix (`http://localhost:6006`, proyecto `ba-explore-api`) se leen el costo por ejecución, la latencia y qué nodo consume más tokens. Las capturas automáticas se generan con:

```bash
pip install playwright
python scripts/capturar_dashboard.py      # usa Edge/Chrome instalado; guarda en screenshots/
```

## Evidencias (`/screenshots`)

> ⚠️ **Pendiente de ejecutar**: las capturas del dashboard y las métricas de la corrida de 5 peticiones requieren Redis y Phoenix en ejecución, y no estaban disponibles al momento de escribir este README. Esta sección se completa con los resultados reales de `load_test.py` y las capturas.

## Seguridad

Las claves se leen de `.env` (en `.gitignore`); solo se sube `.env.example`.

"""Prueba de carga: 5 peticiones concurrentes contra la API.

Cada petición hace POST /tasks (devuelve un job_id al instante) y luego sondea GET /tasks/{id}
hasta que el job termina. Mide la latencia extremo a extremo de cada job y calcula p50/p95.

Uso:
    python scripts/load_test.py                    # 5 consultas concurrentes
    python scripts/load_test.py --url http://localhost:8000 --concurrencia 5
"""
import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

CONSULTAS = [
    "Museos y parrillas en Recoleta a menos de 2 km",
    "Bares en Palermo a 1 km",
    "Museos en San Telmo a 2 km",
    "Restaurantes y cafés cerca del Obelisco a 1 km",
    "Museos y bares en Puerto Madero a 2 km",
]
FINALES = {"DONE", "REJECTED", "FAILED"}


async def una_peticion(cli: httpx.AsyncClient, i: int, query: str) -> dict:
    t0 = time.perf_counter()
    r = await cli.post("/tasks", json={"query": query})
    r.raise_for_status()
    job_id, t_encolado = r.json()["job_id"], time.perf_counter() - t0   # tiempo de respuesta de POST
    while True:
        job = (await cli.get(f"/tasks/{job_id}")).json()
        if job["status"] in FINALES or job["status"] == "AWAITING_APPROVAL":
            break
        await asyncio.sleep(1.0)
    total = time.perf_counter() - t0
    return {"n": i, "job_id": job_id, "query": query, "status": job["status"], "post_ms": round(t_encolado * 1000, 1),
            "latencia_s": round(total, 2), "error": job.get("error")}


async def main(url: str, concurrencia: int) -> None:
    consultas = (CONSULTAS * ((concurrencia // len(CONSULTAS)) + 1))[:concurrencia]
    async with httpx.AsyncClient(base_url=url, timeout=30) as cli:
        health = (await cli.get("/health")).json()
        print(f"API: {health} | lanzando {concurrencia} peticiones concurrentes...\n")
        t0 = time.perf_counter()
        resultados = await asyncio.gather(*(una_peticion(cli, i + 1, q) for i, q in enumerate(consultas)))
        pared = time.perf_counter() - t0

    lat = sorted(r["latencia_s"] for r in resultados)
    p95 = statistics.quantiles(lat, n=100, method="inclusive")[94] if len(lat) > 1 else lat[0]
    for r in resultados:
        print(f"#{r['n']} {r['status']:<8} POST={r['post_ms']:>6} ms  total={r['latencia_s']:>7} s  job={r['job_id']}  {r['query']}")
    resumen = {"concurrencia": concurrencia, "tiempo_total_s": round(pared, 2),
               "ok": sum(r["status"] == "DONE" for r in resultados),
               "fallidos": sum(r["status"] == "FAILED" for r in resultados),
               "latencia_p50_s": round(statistics.median(lat), 2), "latencia_p95_s": round(p95, 2),
               "latencia_max_s": lat[-1], "post_max_ms": max(r["post_ms"] for r in resultados)}
    print("\nRESUMEN:", json.dumps(resumen, ensure_ascii=False))
    (Path(__file__).resolve().parents[1] / "load_test_resultado.json").write_text(
        json.dumps({"resumen": resumen, "peticiones": resultados}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Prueba de carga de la API")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--concurrencia", type=int, default=5)
    a = ap.parse_args()
    asyncio.run(main(a.url, a.concurrencia))

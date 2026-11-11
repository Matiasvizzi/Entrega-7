"""Captura automática del dashboard de Phoenix (evidencia para /screenshots).

Usa Playwright con el Edge/Chrome ya instalado (no descarga navegadores). Debe correrse con
Phoenix ya levantado y DESPUÉS de la prueba de carga, así el dashboard tiene datos.

    pip install playwright
    python scripts/capturar_dashboard.py [--phoenix http://localhost:6006] [--proyecto ba-explore-api]
"""
import argparse
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

SALIDA = Path(__file__).resolve().parents[1] / "screenshots"


def proyecto_id(phoenix: str, nombre: str) -> str:
    consulta = {"query": "{ projects { edges { node { id name } } } }"}
    req = urllib.request.Request(f"{phoenix}/graphql", data=json.dumps(consulta).encode(),
                                 headers={"Content-Type": "application/json"})
    nodos = [e["node"] for e in json.load(urllib.request.urlopen(req))["data"]["projects"]["edges"]]
    return next(n["id"] for n in nodos if n["name"] == nombre)


def main(phoenix: str, proyecto: str) -> None:
    SALIDA.mkdir(exist_ok=True)
    pid = proyecto_id(phoenix, proyecto)
    with sync_playwright() as p:
        navegador = None
        for canal in ("msedge", "chrome"):
            try:
                navegador = p.chromium.launch(channel=canal, headless=True)
                break
            except Exception:
                continue
        if navegador is None:
            raise SystemExit("No se encontró Edge ni Chrome instalados.")
        pagina = navegador.new_page(viewport={"width": 1600, "height": 950})

        def foto(url: str, nombre: str, espera_ms: int = 4000) -> None:
            pagina.goto(url, wait_until="networkidle")
            pagina.wait_for_timeout(espera_ms)
            pagina.screenshot(path=str(SALIDA / nombre))
            print("📸", nombre)

        foto(f"{phoenix}/projects/{pid}", "01_proyecto_resumen_latencia_costo.png")
        foto(f"{phoenix}/projects/{pid}/traces", "02_trazas_lista.png")
        foto(f"{phoenix}/projects/{pid}/sessions", "03_sesiones_por_job.png")

        # Detalle de una traza: abre la primera fila de la tabla (spans de cada nodo del grafo)
        pagina.goto(f"{phoenix}/projects/{pid}/traces", wait_until="networkidle")
        pagina.wait_for_timeout(3000)
        pagina.locator("table tbody tr").first.click()
        pagina.wait_for_timeout(4000)
        pagina.screenshot(path=str(SALIDA / "04_traza_detalle_spans.png"))
        print("📸 04_traza_detalle_spans.png")
        navegador.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phoenix", default="http://localhost:6006")
    ap.add_argument("--proyecto", default="ba-explore-api")
    a = ap.parse_args()
    main(a.phoenix, a.proyecto)

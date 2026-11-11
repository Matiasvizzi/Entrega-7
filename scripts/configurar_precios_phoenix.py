"""Registra en Phoenix el precio por token del modelo Gemini para que calcule el costo por ejecucion.

Phoenix trae precios para modelos conocidos, pero no para el alias `gemini-flash-lite-latest`; sin
esto el dashboard muestra costo $0. Debe correrse ANTES de la prueba de carga (Phoenix calcula el
costo al recibir cada span). Precios en USD por millon de tokens, ajustables por argumento.

    python scripts/configurar_precios_phoenix.py [--phoenix http://localhost:6006] \
        [--modelo gemini-flash-lite-latest] [--entrada 0.10] [--salida 0.40]
"""
import argparse
import json
import urllib.request

MUTACION = """
mutation($input: CreateModelMutationInput!) {
  createModel(input: $input) { model { id name provider } }
}"""


def main(phoenix: str, modelo: str, entrada: float, salida: float) -> None:
    variables = {"input": {
        "name": modelo,
        "provider": "google",
        "namePattern": f"^{modelo}$",
        "costs": [
            {"tokenType": "input", "kind": "PROMPT", "costPerMillionTokens": entrada},
            {"tokenType": "output", "kind": "COMPLETION", "costPerMillionTokens": salida},
        ],
    }}
    req = urllib.request.Request(f"{phoenix}/graphql", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"query": MUTACION, "variables": variables}).encode())
    print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phoenix", default="http://localhost:6006")
    ap.add_argument("--modelo", default="gemini-flash-lite-latest")
    ap.add_argument("--entrada", type=float, default=0.10)
    ap.add_argument("--salida", type=float, default=0.40)
    a = ap.parse_args()
    main(a.phoenix, a.modelo, a.entrada, a.salida)

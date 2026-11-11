import os

# Los tests no deben enviar trazas a Phoenix ni requerir un colector levantado
os.environ["OBSERVABILIDAD"] = "off"

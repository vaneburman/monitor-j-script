#!/usr/bin/env bash
# exit on error
set -o errexit

# Instala las dependencias del requirements.txt
pip install -r requirements.txt

# Descarga el archivo de definición de Prometheus, siguiendo redirecciones (-L)
echo "Descargando prometheus.proto..."
curl -L -o prometheus.proto https://raw.githubusercontent.com/prometheus/prometheus/main/prompb/prometheus.proto

# Genera el archivo python prometheus_pb2.py
echo "Generando prometheus_pb2.py..."
python -m grpc_tools.protoc -I=. --python_out=. prometheus.proto

echo "Build finalizado."


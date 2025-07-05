#!/usr/bin/env bash
# exit on error
set -o errexit

# Instala las dependencias
pip install -r requirements.txt

# Genera el archivo .py a partir del .proto local
echo "Generando prometheus_pb2.py desde el archivo local..."
python -m grpc_tools.protoc -I=. --python_out=. --grpc_python_out=. prometheus.proto

echo "Build finalizado."

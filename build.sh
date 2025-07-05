#!/usr/bin/env bash
set -o errexit
pip install -r requirements.txt
curl -o prometheus.proto https://raw.githubusercontent.com/prometheus/prometheus/main/prompb/prometheus.proto
python -m grpc_tools.protoc -I=. --python_out=. prometheus.proto

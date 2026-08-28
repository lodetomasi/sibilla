#!/bin/bash
# Serve la dashboard eToro read-only su 127.0.0.1:8000 (tunnel SSH, mai esposta pubblicamente).
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH=src
set -a; source data/etoro_secrets.env 2>/dev/null; set +a
exec .venv/bin/python -m uvicorn api.etoro_app:app --host 127.0.0.1 --port 8000

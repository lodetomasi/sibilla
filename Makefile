PY := PYTHONPATH=src .venv/bin/python -u
PIP := .venv/bin/uv pip

.PHONY: install test lint fmt typecheck api worker gen-key initdb check

install:
	uv venv --python 3.12
	uv pip install -e ".[dev]"

gen-key:
	@$(PY) -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

initdb:
	$(PY) -m scripts.initdb

test:
	.venv/bin/pytest -q

test-risk:
	.venv/bin/pytest -q tests/test_risk_iron_rules.py

lint:
	.venv/bin/ruff check src tests

fmt:
	.venv/bin/ruff format src tests
	.venv/bin/ruff check --fix src tests

typecheck:
	.venv/bin/mypy src

api:
	.venv/bin/uvicorn api.etoro_app:app --reload --port 8000

worker:
	$(PY) -m workers.runner

check: lint test

simulate:
	$(PY) scripts/simulate.py --days 5

simulate-live:
	$(PY) scripts/simulate.py --days 5 --live-minutes 30

ig-check:
	$(PY) scripts/ig_check.py

run:
	$(PY) -m workers.runner

check-balance:
	$(PY) scripts/check_balance.py

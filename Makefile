VENV := .venv
PY := $(VENV)/bin/python

.PHONY: install demo api web test test-backend test-frontend e2e

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e "./backend[all]"
	cd frontend && npm install

demo:
	cd backend && ../$(PY) -m qal.cli demo

api:
	cd backend && ../$(VENV)/bin/uvicorn qal.api.main:app --reload --port 8000

web:
	cd frontend && npm run dev

test: test-backend test-frontend

test-backend:
	cd backend && ../$(PY) -m pytest -q

test-frontend:
	cd frontend && npx tsc -b && npm test

e2e:
	cd frontend && npm run e2e

.PHONY: install test lint typecheck compose-config up down logs profiles profile-check

profiles:
	python -m prediction_trader.profile_cli list

profile-check:
	python -m prediction_trader.profile_cli check

install:
	python -m pip install -e '.[dev]'

test:
	pytest -q

lint:
	ruff check .

typecheck:
	mypy

compose-config:
	docker compose --env-file .env -f deploy/compose.yaml config --quiet

up:
	docker compose --env-file .env -f deploy/compose.yaml up --build -d

down:
	docker compose --env-file .env -f deploy/compose.yaml down

logs:
	docker compose --env-file .env -f deploy/compose.yaml logs -f trader

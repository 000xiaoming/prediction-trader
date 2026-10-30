.PHONY: install test lint typecheck check build compose-config doctor up down logs profiles profile-check

PYTHON ?= python3

profiles:
	$(PYTHON) -m prediction_trader.profile_cli list

profile-check:
	$(PYTHON) -m prediction_trader.profile_cli check

install:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy

check: lint typecheck test

build:
	$(PYTHON) -m build

compose-config:
	./pt check

doctor:
	./pt doctor

up:
	./pt start

down:
	./pt down

logs:
	./pt logs -f trader

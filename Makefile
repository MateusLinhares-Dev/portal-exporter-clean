.PHONY: install lint format test run worker scheduler migrate

install:
	python -m pip install -e '.[dev]'

lint:
	ruff check .

format:
	ruff format .

test:
	pytest -q

run:
	uvicorn softexpert_exporter.main:app --reload

worker:
	python -m softexpert_exporter.worker

scheduler:
	python -m softexpert_exporter.scheduler

migrate:
	alembic upgrade head

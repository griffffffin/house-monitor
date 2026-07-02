.PHONY: install install-dev test format lint typecheck check live-check

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

test:
	python3 -m pytest tests/ -v

format:
	black house_monitor/ tests/ scripts/

lint:
	ruff check house_monitor/ tests/ scripts/

typecheck:
	mypy house_monitor/

check: test lint typecheck
	black --check house_monitor/ tests/ scripts/

live-check:
	python3 -m scripts.live_smoke_check

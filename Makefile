.PHONY: install lint test check demo historical verify-historical clean

install:
	python -m pip install -e ".[dev]"

lint:
	python -m ruff format --check src tests scripts
	python -m ruff check src tests scripts

test:
	python -m pytest --cov=crypto_options --cov-report=term-missing --cov-fail-under=85

check: lint test

demo:
	python -m crypto_options.cli demo --out-dir artifacts/reference --paths 250 --steps 180 --seed 17

historical:
	python -m crypto_options.cli backtest-historical

verify-historical:
	python scripts/verify_historical.py

clean:
	@echo "Remove local caches manually; reference artifacts are intentionally preserved."

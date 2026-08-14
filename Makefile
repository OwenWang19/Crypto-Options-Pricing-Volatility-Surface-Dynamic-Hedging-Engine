.PHONY: install lint test check demo clean

install:
	python -m pip install -e ".[dev]"

lint:
	python -m ruff format --check src tests
	python -m ruff check src tests

test:
	python -m pytest --cov=crypto_options --cov-report=term-missing --cov-fail-under=85

check: lint test

demo:
	python -m crypto_options.cli demo --out-dir artifacts/reference --paths 250 --steps 180 --seed 17

clean:
	@echo "Remove local caches manually; reference artifacts are intentionally preserved."

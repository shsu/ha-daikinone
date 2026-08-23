.PHONY: lint format typecheck test test-fast hassfest spec-drift secret-scan check hooks

UV := uv run

lint:
	$(UV) ruff check .
	$(UV) ruff format --check .

format:
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck:
	$(UV) mypy

test:
	$(UV) pytest -q --cov --cov-fail-under=95

test-fast:
	$(UV) pytest -q -x

hassfest:
	$(UV) python scripts/hassfest_local.py

spec-drift:
	$(UV) python scripts/check_spec_drift.py

secret-scan:
	./scripts/secret_scan.sh

hooks:
	$(UV) pre-commit install --hook-type pre-commit --hook-type pre-push

check: lint typecheck test secret-scan hassfest
	@echo "ALL GATES GREEN"

UV_RUN := uv run --locked

.PHONY: check lint fmt typecheck test
check: lint typecheck test

lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

fmt:
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .

typecheck:
	$(UV_RUN) mypy

test:
	$(UV_RUN) pytest

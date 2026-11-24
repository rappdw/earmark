UV_RUN := uv run --locked
FLOOR_RUN := uv run --locked --project tools/py39
# The contract checker's interpreters: the PKA floor (macOS's python3), and one below it
# for the version guard. --no-bin: never put python3.X links in ~/.local/bin.
CHECKER_PYTHONS := 3.9.6 3.8
FIND_PYTHON := uv python find --no-project --managed-python

.PHONY: check lint fmt typecheck test checker-pythons samples
check: lint typecheck test

lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

fmt:
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .

typecheck:
	$(UV_RUN) mypy
	$(FLOOR_RUN) mypy --config-file tools/py39/pyproject.toml plugins

test: checker-pythons
	MAIL_CHECKER_REQUIRE_FLOOR=1 \
	MAIL_CHECKER_PYTHONS="$$($(FIND_PYTHON) 3.9.6):$$($(FIND_PYTHON) 3.8)" \
	$(UV_RUN) pytest

checker-pythons:
	uv python install --no-bin $(CHECKER_PYTHONS)

samples:
	$(UV_RUN) python -m tests.contract_samples

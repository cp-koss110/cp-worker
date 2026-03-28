.PHONY: help install test test-unit test-integration lint pre-commit-install pre-commit-run venv-clean release

VENV       := .venv
PYTHON_BIN := $(shell python3.12 -c "import sys;print(sys.executable)" 2>/dev/null || python3.11 -c "import sys;print(sys.executable)" 2>/dev/null || echo python3)
PYTHON     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF   := $(VENV)/bin/ruff

help:
	@echo ""
	@echo "cp-worker — available targets"
	@echo ""
	@echo "  install              Create .venv and install all dependencies"
	@echo "  test                 Run unit tests (alias for test-unit)"
	@echo "  test-unit            Run unit tests (mocked AWS, fast)"
	@echo "  test-integration     Run integration tests (requires LOCALSTACK_ENDPOINT)"
	@echo "  lint                 Run ruff linter"
	@echo "  pre-commit-install   Install pre-commit git hooks"
	@echo "  pre-commit-run       Run all pre-commit hooks against all files"
	@echo "  venv-clean           Remove .venv"
	@echo "  release TAG=vX.Y.Z   Create + push a git tag to trigger the Release workflow"
	@echo ""

$(VENV)/bin/activate: requirements.txt requirements-dev.txt
	$(PYTHON_BIN) -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt -r requirements-dev.txt
	touch $(VENV)/bin/activate

install: $(VENV)/bin/activate

test: test-unit

test-unit: install
	$(PYTEST) tests/test_worker.py -v --cov=app --cov-report=term-missing

test-integration: install
	@if [ -z "$(LOCALSTACK_ENDPOINT)" ]; then \
		echo "ERROR: LOCALSTACK_ENDPOINT is not set."; \
		echo "  Start LocalStack: cd ../cp-infra && make local-up"; \
		echo "  Then: LOCALSTACK_ENDPOINT=http://localhost:4566 make test-integration"; \
		exit 1; \
	fi
	$(PYTEST) tests/integration/ -v

lint: install
	$(RUFF) check app/ tests/

pre-commit-install: install
	$(VENV)/bin/pre-commit install

pre-commit-run: install
	$(VENV)/bin/pre-commit run --all-files

venv-clean:
	rm -rf $(VENV)

release:
	@if [ -z "$(TAG)" ]; then \
		echo "Usage: make release TAG=v1.2.3"; \
		echo "  Creates an annotated tag and pushes it to origin."; \
		echo "  The Release workflow will build the image and create a GitHub Release."; \
		exit 1; \
	fi
	git tag -a $(TAG) -m "Release $(TAG)"
	git push origin $(TAG)
	@echo "Tag $(TAG) pushed — Release workflow starting."

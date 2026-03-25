.PHONY: install test test-unit test-integration lint venv-clean

VENV       := .venv
PYTHON_BIN := $(shell python3.12 -c "import sys;print(sys.executable)" 2>/dev/null || python3.11 -c "import sys;print(sys.executable)" 2>/dev/null || echo python3)
PYTHON     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF   := $(VENV)/bin/ruff

$(VENV)/bin/activate: requirements.txt requirements-dev.txt
	$(PYTHON_BIN) -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt -r requirements-dev.txt
	touch $(VENV)/bin/activate

install: $(VENV)/bin/activate

test: test-unit

test-unit: install
	$(PYTEST) tests/test_worker.py -v

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

venv-clean:
	rm -rf $(VENV)

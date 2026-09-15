PYTHON := .venv/bin/python
PROTOCOL_PYTHONPATH := packages/protocol/src
PRODUCT_PYTHONPATH := apps/api/src:apps/companion/src:packages/protocol/src:packages/target-state/src

.PHONY: setup setup-python protocol-schema protocol-types test test-contracts test-unit portal-build extension-build api-start start stop pair prepare-capture companion-status reset-demo smoke-pairing smoke-daemon smoke-capture smoke-partial smoke-failed smoke-general smoke-capture-interruption target-setup target-init target-probe target-reset target-start target-stop target-test-session

setup: setup-python
	npm ci --no-audit --no-fund
	$(PYTHON) -m playwright install chromium
	PYTHONPATH=packages/target-state/src $(PYTHON) scripts/setup_target.py
	@if test ! -f .local/target/data/installation.json; then PYTHONPATH=packages/target-state/src $(PYTHON) scripts/target.py init; fi
	$(MAKE) protocol-types portal-build extension-build

setup-python:
	python3.13 -m venv .venv
	$(PYTHON) -m pip install --requirement requirements-dev.lock

protocol-schema:
	PYTHONPATH=$(PROTOCOL_PYTHONPATH) $(PYTHON) packages/protocol/scripts/export_schema.py

protocol-types: protocol-schema
	npm run protocol:types

test-contracts: protocol-types
	PYTHONPATH=$(PROTOCOL_PYTHONPATH) $(PYTHON) -m pytest packages/protocol/tests
	npm run protocol:typecheck
	npm run protocol:test:ts

test-unit:
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) -m pytest apps/api/tests apps/companion/tests packages/protocol/tests packages/target-state/tests

test: test-unit test-contracts
	npm --workspace @workflow/portal run lint
	$(MAKE) portal-build extension-build

portal-build:
	npm run portal:build

extension-build:
	npm run extension:typecheck
	npm run extension:build

api-start: portal-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) -m uvicorn workflow_api.app:app --host 127.0.0.1 --port 8000

start:
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) -m workflow_companion.service start

stop:
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) -m workflow_companion.service stop

pair:
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) -m workflow_companion.service pair

prepare-capture:
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) -m workflow_companion.service prepare-capture

companion-status:
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) -m workflow_companion.service status

reset-demo: target-reset

smoke-pairing: portal-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) scripts/smoke_pairing.py --headed

smoke-daemon: portal-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) scripts/smoke_daemon.py

smoke-capture: portal-build extension-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) scripts/smoke_capture.py --headed --scenario success

smoke-partial: portal-build extension-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) scripts/smoke_capture.py --headed --scenario partial

smoke-failed: portal-build extension-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) scripts/smoke_capture.py --headed --scenario failed

smoke-general: portal-build extension-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) scripts/smoke_general.py --headed

smoke-capture-interruption: portal-build extension-build
	PYTHONPATH=$(PRODUCT_PYTHONPATH) $(PYTHON) scripts/smoke_capture_interruption.py

target-setup:
	PYTHONPATH=packages/target-state/src $(PYTHON) scripts/setup_target.py

target-init:
	PYTHONPATH=packages/target-state/src $(PYTHON) scripts/target.py init

target-probe:
	PYTHONPATH=packages/target-state/src $(PYTHON) scripts/target.py probe --expect-baseline

target-reset:
	PYTHONPATH=packages/target-state/src $(PYTHON) scripts/target.py reset

target-start:
	PYTHONPATH=packages/target-state/src $(PYTHON) scripts/target.py serve

target-stop:
	PYTHONPATH=packages/target-state/src $(PYTHON) scripts/target.py stop

target-test-session:
	WORKFLOW_TARGET_BASELINE=$(CURDIR)/.local/target/data/baseline.sqlite3 PYTHONPATH=target/integration:packages/target-state/src:.local/target/source:.local/target/source/src WORKFLOW_TARGET_DATA_ROOT=$(CURDIR)/.local/target/data WORKFLOW_TARGET_DATABASE=$(CURDIR)/.local/target/data/db.sqlite3 WORKFLOW_TARGET_CONTROL_DATABASE=$(CURDIR)/.local/state/companion.sqlite3 WORKFLOW_TARGET_EXPECTED_HOST=127.0.0.1:8765 WORKFLOW_TARGET_ORIGIN=http://127.0.0.1:8765 WORKFLOW_TARGET_SECRET_KEY_FILE=$(CURDIR)/.local/target/data/django-secret-key DJANGO_SETTINGS_MODULE=workflow_target.settings .local/target/venv/bin/python target/integration_test.py
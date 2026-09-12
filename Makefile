# Debian AI Assistant — task runner.
#
# Everything here is stdlib python3 plus bash: there is no dependency to
# install and no build step.  `make help` lists the targets.
#
# Note: recipes use tabs, as make requires.

PYTHON ?= python3
SHELL  := /bin/bash

SERVICES := services/model-router/server.py services/agent-s-worker/server.py

.DEFAULT_GOAL := help

.PHONY: help check test test-quick lint compile shellcheck doctor smoke \
        up down status logs restart keys models plan config approve tokens \
        skills hatch clean distclean install-dev all

## help: list every target with its description
help:
	@echo "Debian AI Assistant"
	@echo
	@echo "Verification"
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  make /' | \
		awk -F': ' '{printf "%-22s %s\n", $$1, $$2}'
	@echo
	@echo "No dependencies to install; python3 (>=3.9) and bash are enough."

## check: validate both services' configuration without serving
check:
	@for svc in $(SERVICES); do \
		echo "== $$svc"; \
		$(PYTHON) "$$svc" --check || exit 1; \
	done

## test: run the full suite (unit, HTTP integration, shell, consistency)
test:
	$(PYTHON) -m unittest discover -s tests -t .

## test-quick: run only the fast, non-HTTP tests
test-quick:
	$(PYTHON) -m unittest \
		tests.test_redact tests.test_env tests.test_jsonio \
		tests.test_approvals tests.test_routing \
		tests.test_repo_consistency

## compile: byte-compile every python file (a fast syntax gate)
compile:
	$(PYTHON) -m compileall -q lib services skills tests

## lint: everything checkable without third-party tools
lint: compile
	@echo "== bash -n"
	@for f in bin/*.sh bin/dai; do bash -n "$$f" || exit 1; done
	@echo "== service --check"
	@$(MAKE) --no-print-directory check
	@echo
	@echo "shellcheck and ruff are not required; install them for more:"
	@echo "  shellcheck bin/*.sh bin/dai"
	@echo "  ruff check lib services skills tests"

## shellcheck: run shellcheck if it is installed (skipped otherwise)
shellcheck:
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck -x bin/*.sh bin/dai; \
	else \
		echo "shellcheck not installed — skipping (not a dependency)"; \
	fi

## doctor: check files, keys, services and optional GUI dependencies
doctor:
	./bin/doctor.sh

## smoke: end-to-end self-test (suite + live checks when the spine is up)
smoke:
	./bin/smoke.sh

## up: start the spine (router + worker + agent display)
up:
	./bin/start-spine.sh

## down: stop the spine
down:
	./bin/stop-spine.sh

## restart: stop then start the spine
restart: down up

## status: report whether the services are running
status:
	./bin/dai status

## logs: tail the service logs (n=40 by default)
logs:
	./bin/dai logs $(n)

## keys: import provider keys from other local installs into .env
keys:
	./bin/import-keys.sh

## models: list the models the router can serve right now
models:
	./bin/dai models

## plan: show what the router would try, without calling anything (m=model id)
plan:
	./bin/dai plan $(m)

## config: print resolved configuration for both services, secrets removed
config:
	./bin/dai config

## approve: issue a token — make approve a=agent_s_gui_task s="instruction"
approve:
	@test -n "$(a)" || { echo "usage: make approve a=<action> s=<scope> [ttl=3600]"; exit 1; }
	@test -n "$(s)" || { echo "usage: make approve a=<action> s=<scope> [ttl=3600]"; exit 1; }
	./bin/issue-approval.sh "$(a)" "$(s)" $(if $(ttl),--ttl $(ttl))

## tokens: list approval tokens (masked)
tokens:
	./bin/issue-approval.sh --list

## skills: install the spine skills into the Vellum workspace
skills:
	./bin/install-vellum-skill.sh

## hatch: hatch the Vellum assistant (refuses without a ready inference path)
hatch:
	./bin/hatch-vellum.sh

## clean: remove caches and generated python artifacts
clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.py[co]' -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache

## distclean: clean, plus logs and runtime state (never touches .env or tokens)
distclean: clean
	rm -rf logs state skills-ready

## install-dev: install optional linters into a venv (not required to run)
install-dev:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip ruff shellcheck-py
	@echo "activate with: source .venv/bin/activate"

## all: the full pre-commit gate
all: lint test smoke

# Prefer the worktree venv (created by the orca.yaml setup hook) when present.
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: help test lint install-dev brain-install bench-serve eval eval-gate evolve audit-verify ui

help: ## List targets
	@grep -E '^[a-z][a-z0-9_-]*:.*##' Makefile | sed 's/:.*##/ —/'

install-dev: ## Editable install with dev extras
	@$(PY) -m pip install -e '.[dev]'

brain-install: ## Install the sibling slos-recall checkout so the brain runs in-process
	@$(PY) -m pip install -e ../slos-recall

lint: ## Ruff over the package and the eval top level (eval/tasks fixtures are meant to be buggy)
	@$(PY) -m ruff check coding_harness/ eval/*.py

audit-verify: ## Verify the tool-call audit chain; nonzero exit on tamper (AUDIT_ARGS='--path X')
	@$(PY) -m coding_harness.security.audit $(AUDIT_ARGS)

ui: ## Build the web UI into coding_harness/ui/dist, which `bjorn serve` serves
	@cd coding_harness/ui && npm install --no-audit --no-fund --silent && npm run build

eval: ## Run the harness eval and pin a baseline
	@$(PY) eval/run.py $(EVAL_ARGS)

eval-gate: ## Regression gate vs pinned baseline; nonzero exit on regression
	@$(PY) eval/gate.py $(GATE_ARGS)

evolve: ## Prompt evolution over the overlay block; writes eval/results/overlay-best.md
	@$(PY) eval/evolve.py $(EVOLVE_ARGS)

bench-serve: ## Ollama serving bench (P0-0): load, prefill/decode tok/s, cache, queue
	@$(PY) eval/bench_serve.py $(BENCH_ARGS)

test: ## Ruff + per-file tests (per-file dodges a cross-test teardown SIGPIPE)
	@$(PY) -m ruff check coding_harness/ eval/*.py
	@fail=0; n=0; \
	for f in coding_harness/tests/test_*.py eval/test_*.py; do \
	  n=$$((n+1)); \
	  out=$$($(PY) -m pytest "$$f" -q --tb=short -p no:cacheprovider 2>&1); rc=$$?; \
	  if [ $$rc -ne 0 ] && [ $$rc -ne 5 ]; then echo "FAIL: $$f"; echo "$$out" | tail -40; fail=1; fi; \
	done; \
	if [ $$fail -eq 0 ]; then echo "bjorn-harness: $$n test files pass, ruff clean"; else exit 1; fi

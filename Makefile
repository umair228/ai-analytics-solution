# DIS — dev & CI tasks.
VENV ?= .venv/bin
PY    = $(VENV)/python

.PHONY: check test eval eval-domain ci build up down seed-demo seed-users

check:        ## Django system check
	$(PY) manage.py check

seed-demo:    ## Seed the full oil & gas demo (users, datasets, dashboards, alerts, notifications, reports)
	$(PY) manage.py seed_demo $(ARGS)

seed-users:   ## Seed only the refinery org/site/section-labs and user roster
	$(PY) manage.py seed_lab $(ARGS)

test:         ## Deterministic unit + golden + authz tests (analytics, agent loop, NL->SQL, docsearch/RAG authz, executor, filter-heal)
	$(PY) manage.py test analytics ai docsearch querybuilder datasets -v1

eval:         ## ASTM/ISO retrieval eval (needs the doc index built)
	$(PY) manage.py eval_astm

eval-domain:  ## Compare a served model on domain Qs (needs an LLM endpoint)
	$(PY) finetune/eval_domain.py --base-url $${LLM_BASE_URL:-http://127.0.0.1:11434/v1} --model $${LLM_MODEL:-qwen2.5:7b-instruct}

ci: check test   ## The CI accuracy gate (deterministic — no GPU/LLM required)
	@echo "✓ CI gate passed: django check + analytics/ai/NL->SQL golden + docsearch/RAG authz tests."

build:        ## Build all container images
	docker compose build

up:           ## Start the stack (add 'gpu' profile for local vLLM: make up ARGS=--profile gpu)
	docker compose $(ARGS) up -d

down:
	docker compose down

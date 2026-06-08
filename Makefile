# DIS — dev & CI tasks.
VENV ?= .venv/bin
PY    = $(VENV)/python

.PHONY: check test eval eval-domain ci build up down

check:        ## Django system check
	$(PY) manage.py check

test:         ## Deterministic unit + golden tests (analytics engines, agent loop, NL->SQL)
	$(PY) manage.py test analytics ai -v1

eval:         ## ASTM/ISO retrieval eval (needs the doc index built)
	$(PY) manage.py eval_astm

eval-domain:  ## Compare a served model on domain Qs (needs an LLM endpoint)
	$(PY) finetune/eval_domain.py --base-url $${LLM_BASE_URL:-http://127.0.0.1:11434/v1} --model $${LLM_MODEL:-qwen2.5:7b-instruct}

ci: check test   ## The CI accuracy gate (deterministic — no GPU/LLM required)
	@echo "✓ CI gate passed: django check + analytics/ai/NL->SQL golden tests."

build:        ## Build all container images
	docker compose build

up:           ## Start the stack (add 'gpu' profile for local vLLM: make up ARGS=--profile gpu)
	docker compose $(ARGS) up -d

down:
	docker compose down

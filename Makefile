SHELL := /bin/bash
COMPOSE_FILES := $(shell ls docker-compose.yml docker-compose.ws*.yml 2>/dev/null)
COMPOSE_ARGS := $(foreach f,$(COMPOSE_FILES),-f $(f))
PACKAGES := packages/contracts packages/dse_audit packages/dse_identity
SERVICES := $(wildcard services/*)

.PHONY: up down migrate test test-direct test-contracts test-list install lint logs ps

up:
	docker compose $(COMPOSE_ARGS) up -d
	@echo "Aguardando Postgres..."
	@until docker exec dse_postgres pg_isready -U dse -d dse >/dev/null 2>&1; do sleep 1; done
	@echo "Infra no ar. Temporal UI: http://localhost:8088  Vault: http://localhost:8200"

down:
	docker compose $(COMPOSE_ARGS) down

ps:
	docker compose $(COMPOSE_ARGS) ps

logs:
	docker compose $(COMPOSE_ARGS) logs -f

migrate:
	python3 scripts/migrate.py

install:
	@for p in $(PACKAGES); do \
		if [ -d $$p ]; then echo "installing $$p"; pip install -e $$p --quiet || exit 1; fi; \
	done
	@for s in $(SERVICES); do \
		if [ -f $$s/pyproject.toml ]; then echo "installing $$s"; pip install -e $$s --quiet || exit 1; fi; \
	done

test:
	python3 scripts/with_test_database.py -- python3 scripts/test_matrix.py

test-direct:
	python3 scripts/test_matrix.py

test-contracts:
	python3 scripts/test_matrix.py --group contracts

test-list:
	python3 scripts/test_matrix.py --list

lint:
	python3 -m py_compile $$(find packages services -name '*.py' \
		-not -path '*/node_modules/*' -not -path '*/.venv*/*' \
		-not -path '*/preview_repo/*')
	ruff check packages services scripts/test_matrix.py

# Imagem do sandbox com o agent-runner (Fase 1 — turno isolado). Com ela
# construída, DSE_SANDBOX_IMAGE=dse/agent-runner:local DSE_SANDBOX_INPROCESS=0
# roda o turno do Coder DENTRO do container (e os testes live de exec ligam).
agent-runner-image:
	docker build -f services/sandbox-runtime/agent-runner/Dockerfile -t dse/agent-runner:local .

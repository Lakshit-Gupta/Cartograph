.PHONY: help install fmt lint type test up down logs migrate migrate-test seed reset psql redis-cli backup restore-drill bootstrap-pi audit-deps

ROOT := /home/lakshit_gupta/coding/cartograph
SOPS_ENV := sops exec-env secrets.yaml

help:
	@echo "cartograph targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' Makefile | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

install: ## uv sync (creates .venv)
	uv sync --all-extras

fmt: ## ruff format
	uv run ruff format src tests

lint: ## ruff check
	uv run ruff check src tests

type: ## mypy
	uv run mypy src

test: ## pytest
	uv run pytest

up: ## sops exec-env + docker compose up -d
	$(SOPS_ENV) 'docker compose up -d'

down: ## docker compose down
	docker compose down

logs: ## tail compose logs
	docker compose logs -f --tail=200

ps: ## compose ps
	docker compose ps

migrate: ## run pending migrations against postgres
	$(SOPS_ENV) 'docker compose run --rm tools python -m src.cli.main migrate'

migrate-test: ## replay migrations on ephemeral pgvector container (no prod touch)
	bash scripts/validate_migrations.sh

seed: ## seed sources from config/sources/*.yaml
	$(SOPS_ENV) 'docker compose run --rm tools python -m src.cli.main seed-sources'

reset: ## DANGEROUS: drop db + redis volumes (dev only)
	docker compose down -v

psql: ## psql shell
	docker compose exec postgres psql -U marked -d marked

redis-cli: ## redis-cli (auth from SOPS)
	$(SOPS_ENV) 'docker compose exec -e REDISCLI_AUTH=$$redis_password redis redis-cli'

backup: ## pg_dump | age encrypt | rclone → R2
	bash scripts/backup.sh

restore-drill: ## restore latest backup into tmpfs (weekly check)
	bash scripts/restore_drill.sh

bootstrap-pi: ## first-time Pi bootstrap (run AS PI USER on the Pi only)
	bash scripts/bootstrap.sh

pretest-cf: ## run CF clearance pretest
	bash scripts/pretest_cf.sh

audit-deps: ## verify every declared dep is actually imported somewhere under src/
	uv run python scripts/audit_deps.py

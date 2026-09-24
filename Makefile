.PHONY: setup up down extract load ask test lint

setup:            ## install dependencies
	uv sync

up:               ## start FalkorDB
	docker compose up -d

down:
	docker compose down

extract:          ## documents -> structured transactions
	uv run screening-graph extract

load:             ## structured transactions -> Graphiti
	uv run screening-graph load

ask:              ## answer the questions in QUESTIONS.md from the graph
	uv run screening-graph ask --all

test:
	uv run pytest

lint:
	uv run ruff check .

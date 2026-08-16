.DEFAULT_GOAL := help
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
IMAGE ?= instagram-username-finder

.PHONY: help install test lint format typecheck check build docker-build docker-run clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create a virtualenv and install the package with dev extras
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

test: ## Run the test suite
	$(BIN)/pytest

lint: ## Run Ruff
	$(BIN)/ruff check .

format: ## Apply Ruff formatting and autofixes
	$(BIN)/ruff format .
	$(BIN)/ruff check . --fix

typecheck: ## Run MyPy over src/
	$(BIN)/mypy src

check: lint typecheck test ## Run every quality gate

build: ## Build the sdist and wheel
	$(BIN)/python -m build

docker-build: ## Build the Docker image
	docker build -t $(IMAGE) .

docker-run: ## Run a small scan in Docker
	docker run --rm -v $(PWD)/data:/app/data $(IMAGE) \
		scan --min-length 3 --max-length 3 --charset letters --max-checks 25

clean: ## Remove build and test artefacts
	rm -rf dist build .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

.DEFAULT_GOAL := help

.PHONY: .uv
.uv: ## Check that uv is installed
	@uv --version || echo 'Please install uv: https://docs.astral.sh/uv/getting-started/installation/'

.PHONY: .pre-commit
.pre-commit: ## Check that pre-commit is installed
	@uv run pre-commit -V || echo 'Please install pre-commit: https://pre-commit.com/'

.PHONY: install
install: .uv .pre-commit ## Install the package, dependencies, and pre-commit hooks
	uv sync --all-groups
	@if git rev-parse --git-dir >/dev/null 2>&1; then \
		uv run pre-commit install --install-hooks; \
	else \
		echo "Skipping pre-commit hook installation: this directory is not a Git repository"; \
	fi

.PHONY: sync
sync: .uv ## Update local packages and uv.lock
	uv sync --all-groups

.PHONY: format
format: ## Format the code and apply safe lint fixes
	uv run ruff format
	uv run ruff check --fix --fix-only

.PHONY: lint
lint: ## Check formatting and lint rules
	uv run ruff format --check
	uv run ruff check

.PHONY: typecheck-pyright
typecheck-pyright: ## Run static type checking with Pyright
	PYRIGHT_PYTHON_IGNORE_WARNINGS=1 uv run pyright

.PHONY: typecheck
typecheck: typecheck-pyright ## Run static type checking

.PHONY: test
test: ## Run tests and collect coverage data
	uv run coverage run -m pytest
	@uv run coverage report

.PHONY: testcov
testcov: test ## Run tests and generate an HTML coverage report
	@echo "building coverage html"
	@uv run coverage html

.PHONY: all
all: format lint typecheck testcov ## Run formatting, linting, type checks, and tests

.PHONY: help
help: ## Show this help (usage: make help)
	@echo "Usage: make [recipe]"
	@echo "Recipes:"
	@awk '/^[a-zA-Z0-9_-]+:.*?##/ { \
		helpMessage = match($$0, /## (.*)/); \
		if (helpMessage) { \
			recipe = $$1; \
			sub(/:/, "", recipe); \
			printf "  \033[36m%-20s\033[0m %s\n", recipe, substr($$0, RSTART + 3, RLENGTH); \
		} \
	}' $(MAKEFILE_LIST)

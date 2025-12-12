.PHONY: help init build test run clean lint format black security type-check

# Colors for pretty printing
ESC := $(shell printf '\033')
COLOR_RESET := $(ESC)[0m
COLOR_BOLD := $(ESC)[1m
COLOR_GREEN := $(ESC)[32m
COLOR_RED := $(ESC)[31m
COLOR_YELLOW := $(ESC)[33m

help:
	@echo "$(COLOR_BOLD)ZTCard Grid Simulator - Makefile$(COLOR_RESET)"
	@echo ""
	@echo "$(COLOR_BOLD)Available targets:$(COLOR_RESET)"
	@echo "  init       - Create conda environment (alias for install)"
	@echo "  build      - Create conda environment (alias for install)"
	@echo "  install    - Create conda environment and install dependencies"
	@echo "  test       - Run tests"
	@echo "  run        - Run the simulator"
	@echo "  clean      - Remove generated files"
	@echo ""
	@echo "$(COLOR_BOLD)Development Commands:$(COLOR_RESET)"
	@echo "  black      - Format Python code using black"
	@echo "  lint       - Run code quality checks (black + mypy)"
	@echo "  type-check - Run type checking with mypy"
	@echo "  security   - Run security checks (bandit + pip-audit)"

# Check for micromamba installation
ifeq ($(MAMBA_EXE),)
    $(error $(COLOR_RED)MAMBA_EXE is not set! Please install micromamba or set MAMBA_EXE environment variable$(COLOR_RESET))
endif

ENV_NAME := grid-simulator
MICROMAMBA_DEV := $(MAMBA_EXE) run -n $(ENV_NAME)-dev
MICROMAMBA_PROD := $(MAMBA_EXE) run -n $(ENV_NAME) 

# Detect which conda tool is available (for backward compatibility)
CONDA := $(shell command -v micromamba 2> /dev/null)
ifndef CONDA
	CONDA := $(shell command -v conda 2> /dev/null)
endif

init: environment-dev.yml
	@echo "$(COLOR_BOLD)Creating development environment...$(COLOR_RESET)"
	@$(MAMBA_EXE) create -yf environment-dev.yml
	@$(MAMBA_EXE) install -n $(ENV_NAME)-dev -yf environment.yml
	@echo "$(COLOR_BOLD)Installing git pre-commit hook...$(COLOR_RESET)"
	@mkdir -p .git/hooks
	@cp .git-hooks/pre-commit .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "$(COLOR_GREEN)Development environment created successfully!$(COLOR_RESET)"
	@echo "$(COLOR_GREEN)Git pre-commit hook installed - black will run automatically before commits$(COLOR_RESET)"

build: environment.yml
	@echo "$(COLOR_BOLD)Setting up production environment...$(COLOR_RESET)"
	@$(MAMBA_EXE) create -yf environment.yml

install-dev: environment-dev.yml
	@echo "$(COLOR_BOLD)Creating development environment...$(COLOR_RESET)"
	@$(MAMBA_EXE) create -yf environment-dev.yml
	@echo "$(COLOR_GREEN)Development environment created successfully!$(COLOR_RESET)"

# Development commands
black:
	@echo "$(COLOR_BOLD)Formatting Python code...$(COLOR_RESET)"
	@${MICROMAMBA_DEV} black -q src/
	@echo "$(COLOR_GREEN)Formatting complete!$(COLOR_RESET)"

lint:
	@echo "$(COLOR_BOLD)Running code quality checks...$(COLOR_RESET)"
	@${MICROMAMBA_DEV} black --check --diff -q src/
	@echo "$(COLOR_GREEN)Code quality checks passed!$(COLOR_RESET)"

type-check:
	@echo "$(COLOR_BOLD)Running type checks...$(COLOR_RESET)"
	@${MICROMAMBA_DEV} mypy --ignore-missing-imports --namespace-packages --explicit-package-bases --exclude tests --check-untyped-defs --warn-redundant-casts --warn-unused-ignores --no-implicit-optional src/
	@echo "$(COLOR_GREEN)Type checking complete!$(COLOR_RESET)"

security:
	@echo "$(COLOR_BOLD)Running security checks...$(COLOR_RESET)"
	@${MICROMAMBA_DEV} bandit -q -lll -ii -r src/ || true
	@${MICROMAMBA_DEV} pip-audit --skip-editable || true
	@echo "$(COLOR_GREEN)Security checks completed!$(COLOR_RESET)"

test:
	@echo "$(COLOR_BOLD)Running tests...$(COLOR_RESET)"
	@PYTHONPATH=src ${MICROMAMBA_DEV} python -m pytest tests/ -v
	@echo "$(COLOR_GREEN)Tests complete!$(COLOR_RESET)"

pipbuild:
	@echo "$(COLOR_BOLD)Building distributable packages...$(COLOR_RESET)"
	@rm -rf build/ dist/ python/citadel-simulator.egg-info/
	@${MICROMAMBA_DEV} python -m build --wheel --sdist
	@echo "$(COLOR_GREEN)Build complete!$(COLOR_RESET)"

# Run the simulator

run:
	@echo "$(COLOR_BOLD)Starting grid simulator...$(COLOR_RESET)"
	@cd src && ${MICROMAMBA_PROD} python -m main

# Clean generated files and environments

clean:
	@echo "$(COLOR_BOLD)Cleaning generated files...$(COLOR_RESET)"
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete
	@find . -type f -name "*.pyo" -delete
	@find . -type f -name "*.log" -delete
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache
	@$(MAMBA_EXE) env remove -yn $(ENV_NAME)-dev || echo "$(COLOR_YELLOW)No dev environment to remove$(COLOR_RESET)"
	@$(MAMBA_EXE) env remove -yn $(ENV_NAME) || echo "$(COLOR_YELLOW)No prod environment to remove$(COLOR_RESET)"
	@echo "$(COLOR_GREEN)Clean complete!$(COLOR_RESET)"

# Error handling for missing files
environment.yml:
	@echo "$(COLOR_RED)environment.yml is missing!$(COLOR_RESET)"
	@echo "This file is required for setting up the production environment."
	@echo "Please ensure it exists in the project root directory."
	@exit 1

environment-dev.yml:
	@echo "$(COLOR_RED)environment-dev.yml is missing!$(COLOR_RESET)"
	@echo "This file is required for setting up the development environment."
	@echo "Please ensure it exists in the project root directory."
	@exit 1
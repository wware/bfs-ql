#!/bin/bash -e

fixes_needed() {
    echo "Something needs fixing, trying to fix it"
    set -x
    uv run black bfsql tests
    uv run ruff check --fix bfsql tests
    exit 1
}

mypy_fix_needed() {
    echo "Something mypy-ish needs fixing. You need to do that."
    exit 1
}

echo "=========================================="
echo "Running Linters and Tests"
echo "=========================================="

# Ensure uv is available
if ! command -v uv &> /dev/null; then
    echo "Error: uv not found. Please install uv first."
    echo "See: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

echo ""
echo "UV Version:"
uv --version

echo ""
echo "=========================================="
echo "Running ruff check..."
echo "=========================================="
uv run ruff check bfsql tests || fixes_needed

echo ""
echo "=========================================="
echo "Running mypy..."
echo "=========================================="
uv run mypy bfsql tests || mypy_fix_needed

echo ""
echo "=========================================="
echo "Running black check..."
echo "=========================================="
uv run black --check bfsql tests || fixes_needed

echo ""
echo "=========================================="
echo "Running tests..."
echo "=========================================="
uv run pytest -v

#!/bin/bash
# Pre-commit hook: refuse to commit secrets, API hashes, phone numbers, etc.
#
# Installs to .git/hooks/pre-commit (executable).
# Called by git before each commit. Exit 1 blocks the commit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Get list of staged files (text files only)
FILES=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(py|sh|yaml|yml|conf|txt|md|json|toml)$' || true)

if [ -z "$FILES" ]; then
    exit 0
fi

# Run the Python secret checker
python3 "$PROJECT_ROOT/core/secrets_check.py" $FILES
exit $?

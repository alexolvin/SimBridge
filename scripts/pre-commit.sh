#!/bin/bash
# Pre-commit hook: refuse commits that contain secrets (Rule 5).
#
# Layer 1: filename check — staging a file that IS a secret (*.session,
#          .env, private key, ...) is always blocked.
# Layer 2: content scan   — high-confidence secret shapes block the commit;
#          low-confidence signals (e.g. .session path references) print
#          warnings.
#
# Install:  cp scripts/pre-commit.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
#          (or: ln -sf ../../scripts/pre-commit.sh .git/hooks/pre-commit)
#
# False positive: put '# SECRET_CHECK_IGNORE' on the line, or add the
#          verified-fake value to config/secret_check_allowlist.txt.

set -euo pipefail

# Works both from scripts/ and once installed in .git/hooks/.
PROJECT_ROOT="$(git rev-parse --show-toplevel)"

# NOTE: unquoted $STAGED below is intentional word-splitting; filenames
# containing spaces are not supported by this hook.
STAGED="$(git diff --cached --name-only --diff-filter=ACM || true)"

if [ -z "$STAGED" ]; then
    exit 0
fi

python3 "$PROJECT_ROOT/core/secrets_check.py" $STAGED

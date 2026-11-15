#!/usr/bin/env bash
# Local validation gate. Mirrors the cheap parts of CI without installing anything,
# touching the network or deploying. Run from anywhere; paths are resolved from the
# repository root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if command -v python3 >/dev/null 2>&1; then
	PYTHON=python3
elif command -v python >/dev/null 2>&1; then
	PYTHON=python
else
	echo "validate: python3 (or python) is required" >&2
	exit 1
fi

echo "validate: bootstrap tests (standard library only)"
(
	cd bootstrap
	"$PYTHON" -W error::ResourceWarning -m unittest discover -s tests -t . -v
)

echo "validate: shell syntax"
for file in setup.sh scripts/*.sh; do
	bash -n "$file"
done

echo "validate: whitespace"
git diff --check
git diff --cached --check
if git rev-parse --verify --quiet origin/main >/dev/null; then
	base="$(git merge-base HEAD origin/main)"
	git diff --check "$base" HEAD
fi

echo "validate: ok"

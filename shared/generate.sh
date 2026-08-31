#!/bin/sh
# Re-Vera — regenerate the shared contract into both languages.
#
#   source of truth   shared/schema.json
#   outputs (both committed)
#     backend/app/schema_models.py    Pydantic v2 models
#     extension/src/types/schema.ts   TypeScript types
#
# Run it from anywhere in the repo:  ./shared/generate.sh
# Re-run it after every edit to shared/schema.json and commit both outputs
# together with the schema change.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

SCHEMA="shared/schema.json"
PY_OUT="backend/app/schema_models.py"
TS_OUT="extension/src/types/schema.ts"

PY_HEADER='# GENERATED — do not hand-edit, run shared/generate.sh
# Source of truth: shared/schema.json'

TS_HEADER='/* eslint-disable */
/**
 * GENERATED — do not hand-edit, run shared/generate.sh
 * Source of truth: shared/schema.json
 */'

if [ ! -f "$SCHEMA" ]; then
  echo "generate.sh: $SCHEMA not found (expected repo root at $ROOT)" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "generate.sh: 'uv' not found — it runs datamodel-code-generator for the Pydantic models." >&2
  echo "             Install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 1
fi

if ! command -v pnpm >/dev/null 2>&1; then
  echo "generate.sh: 'pnpm' not found — it runs json2ts for the TypeScript types." >&2
  echo "             Install it from https://pnpm.io/installation and re-run." >&2
  exit 1
fi

mkdir -p "$(dirname "$PY_OUT")" "$(dirname "$TS_OUT")"

echo "generate.sh: $SCHEMA -> $PY_OUT"
if ! uv run --with datamodel-code-generator datamodel-codegen \
  --input "$SCHEMA" \
  --input-file-type jsonschema \
  --output "$PY_OUT" \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.12 \
  --disable-timestamp \
  --field-constraints \
  --custom-file-header "$PY_HEADER"; then
  echo "generate.sh: datamodel-codegen failed. Check that PyPI is reachable;" >&2
  echo "             'uv run --with datamodel-code-generator datamodel-codegen --help' should work." >&2
  exit 1
fi

echo "generate.sh: $SCHEMA -> $TS_OUT"
if ! pnpm --dir extension exec json2ts \
  --input "$ROOT/$SCHEMA" \
  --output "$ROOT/$TS_OUT" \
  --bannerComment "$TS_HEADER"; then
  echo "generate.sh: json2ts failed. Is json-schema-to-typescript installed in extension/?" >&2
  echo "             pnpm --dir extension add -D json-schema-to-typescript" >&2
  exit 1
fi

echo "generate.sh: done — review the diff in $PY_OUT and $TS_OUT before committing."

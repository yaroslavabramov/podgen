#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$repo_root"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$repo_root/backend/src:$repo_root/backend"
export PODGEN_DATABASE_URL='postgresql+psycopg://runner@/postgres?host=/tmp/pgn-78e2a792-&port=55449'
data="$repo_root/tmp/hosted-planning-2026-11-13"
artifact="$repo_root/evals/reports/2026-11-13-hosted-planning/runtime"
run_id='293c8b5a-53df-5483-92ec-80c9bd7f2258'
pgn=("$repo_root/backend/.venv/bin/python" -m podgen.writing_cli
  --env-file "$repo_root/.env"
  --project 30619987-72ed-4e7e-bd53-3db4b55b64ed
  --actor 568c8069-b91a-4966-bbfe-35e2e0e63eb2
  --objects "$repo_root/tmp/hosted-research-2026-11-13/objects")
case "${1:-preview}" in
  prepare)
    "${pgn[@]}" research-materials-prepare --input "$artifact/material-preparation.json" --out "$data/shared-ref.json"
    ;;
  preview)
    "${pgn[@]}" preview --shared-ref "@$data/shared-ref.json" --case marine-longitude \
      --stage plan --profile "@$data/profile-planner.json" --run-id "$run_id" --out "$data/plan-preview.json"
    ;;
  run)
    "${pgn[@]}" run --run-id "$run_id" --case marine-longitude --author-ref "@$data/shared-ref.json" \
      --writer-profile "@$data/profile-author.json" --reviewer-profile "@$data/profile-editor.json" \
      --planner-profile "@$data/profile-planner.json" --plan-reviewer-profile "@$data/profile-editor.json" \
      --export-dir "$data/export"
    ;;
  resume|inspect)
    "${pgn[@]}" "$1" --run-id "$run_id" --export-dir "$data/export"
    ;;
  *) printf 'Usage: %s prepare|preview|run|resume|inspect\n' "$0" >&2; exit 2 ;;
esac

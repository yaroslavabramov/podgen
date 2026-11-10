#!/usr/bin/env bash
# Use the ordinary source and packet import commands with source paths relative to this case.
set -euo pipefail
: "${PROJECT_ID:?}" "${ACTOR_ID:?}" "${OBJECTS_DIR:?}" "${IMPORT_DIR:?}"
case_dir="$(cd "$(dirname "$0")" && pwd)"
repo="$(git -C "$case_dir" rev-parse --show-toplevel)"
export PYTHONPATH="$repo/backend/src${PYTHONPATH:+:$PYTHONPATH}"
cli=("$repo/backend/.venv/bin/python" -m podgen.writing_cli
     --project "$PROJECT_ID" --actor "$ACTOR_ID" --objects "$OBJECTS_DIR")
mkdir -p "$IMPORT_DIR"
rows=()
while IFS=$'\t' read -r id purpose path; do
  ref="$IMPORT_DIR/$id-ref.json"
  "${cli[@]}" editorial-source-import --file "$case_dir/$path" --source-path "$path" \
    --purpose "$purpose" --scope vereskovaya-evenings --out "$ref"
  rows+=("$(jq -cn --arg path "$path" --slurpfile ref "$ref" '{($path): $ref[0]}')")
done < <(jq -r '.documents[] | [.key, .purpose, .source_path] | @tsv' "$case_dir/shared.json")
printf '%s\n' "${rows[@]}" | jq -s add > "$IMPORT_DIR/source-map.json"
"${cli[@]}" editorial-import --input "$case_dir/shared.json" \
  --source-map "$IMPORT_DIR/source-map.json" --out "$IMPORT_DIR/shared-ref.json"

# Card identity and material references

Every `editorial-input/2` card has a `card_id`, a short `label` and a descriptive `title`. Only the ID must be unique in the dossier. Assign it once when creating the card. Both names can be edited, repeated or reordered. The importer rejects missing, null, duplicate and unrecognized IDs. A v1 input still uses its label as its key and does not accept `card_id`.

The names `summary_support_labels`, `support_labels` and `material_refs` are retained for compatibility. Their values in v2 are card IDs. A document key, authorized source reference, source digest and exact `{document,start,end,text}` quotation identify the underlying evidence separately. The writer sees selected material with its attribution. The persisted review keeps the exact card and document bindings.

## Create, upgrade or rebind

The local tool writes new files and refuses to overwrite existing ones. It works without a database or provider. From the repository root:

```sh
export CARD_DIR="$(pwd)/tmp/card-upgrade"
mkdir -p "$CARD_DIR"
uv run --directory backend python -m podgen.editorial_tools upgrade \
  --input ../evals/editorial-inputs/v1/gps-clock/shared.json \
  --out "$CARD_DIR/shared-v2.json" --map-out "$CARD_DIR/card-map.json"
uv run --directory backend python -m podgen.editorial_tools upgrade-fixed \
  --input ../evals/editorial-inputs/v1/gps-clock/fixed-plan.json \
  --shared "$CARD_DIR/shared-v2.json" --out "$CARD_DIR/fixed-v2.json"
```

`upgrade` preserves names, claims and quotations, assigns IDs, rewrites structured references and records the original key mapping and input digests. `upgrade-fixed` checks that the reconstructed v1 dossier matches the fixed plan's original digest before writing the v2 plan.

To add a card, prepare its full content and citations in `new-card.json` and omit `card_id`. To reuse a fixed plan after renaming or reordering cards, supply both the old and updated dossier:

```sh
uv run --directory backend python -m podgen.editorial_tools add-card \
  --input "$CARD_DIR/shared-v2.json" --card "$CARD_DIR/new-card.json" \
  --out "$CARD_DIR/shared-next.json"
uv run --directory backend python -m podgen.editorial_tools rebind-fixed \
  --input "$CARD_DIR/fixed-v2.json" --previous-shared "$CARD_DIR/shared-v2.json" \
  --shared "$CARD_DIR/shared-renamed.json" --out "$CARD_DIR/fixed-renamed.json"
```

`rebind-fixed` verifies the original digest, IDs and every field except card order and display names. Changes to facts, documents or evidence require a newly bound plan. An author can also create v2 packets with explicit IDs directly against the [published schema](editorial-input.v2.schema.json). Import never generates missing IDs.

## What the model sees

Starting with `editorial-role-projections/5`, model-facing v2 requests use short references such as `M1`, `M2` and `M3`. The run freezes the dossier digest and the exact `model-material-references/1` mapping to persistent card IDs. Preview displays that mapping separately. It applies to structured fields only. Text and quotations are never scanned for an identifier and rewritten. The server rejects a reference outside the offered catalog.

In the archive, `wire_output` is the structured model response and `output` is its validated representation with persistent IDs. The original provider response is preserved. Human-readable output can show display names and disambiguate duplicates. Reordering cards for a new dossier has no effect on an already bound run.

The optional v2 `story_facts` field supplies a card's narrative account when available. Older cards continue to supply their claim and context. Source passages and limitations stay available in the complete dossier. [Current packets](../../evals/editorial-inputs/current/) and [original v1 inputs](../../evals/editorial-inputs/v1/) illustrate both forms.

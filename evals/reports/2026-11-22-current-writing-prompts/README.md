# One active writing-prompt tree

Baseline: `82c36d20`. No paid API or external model call was made.

`writing/prompts/current/` now owns all active production instructions: thirteen
named roles, shared common/format blocks, material reading, operational research,
retained-source tools and the Russian craft reference. The runtime uses explicit
role/format/source context. Markdown conditions select short source-dependent
wording inside its owning role; named sections express reuse. Python does not
find or replace English sentences and does not scan possible current prompts to
identify an archived request.

Fresh prompt identities hash the complete actual instruction text, including
selected operational blocks. Saved preview/resume keep their manifest bodies
and identities. Resolving behavior is lazy: it does not open current or historical
prompt files merely to resume a saved request. Archived experimental adaptations
use exact fingerprints, with role and format bound for research reading. Unknown
or changed archived inputs fail explicitly.

New research manifests pin exactly one source-only instruction variant, for
initial `fact-review`, bound to the original complete instructions. It shares a
bounded Markdown snapshot with the original role. Report-off can therefore be
prepared after arbitrary current wording edits without growing a frozen index.
The variant remains outside model context; neither unused recheck variants nor
a new role-identification scan are stored.

## Exact baseline evidence

The original backend is extracted outside the checkout at
`/tmp/podgen-prompts-82c36d20/`. A second isolated copy contains that same backend
with only the new writing loader/resources. Its old tool-selection policy remains
unchanged; the navigation attachment has only its new loader API call.

All **366 records match by complete text** before intentional changes to factual
rechecking or role tools: 324 role/format/profile instruction records, 22 complete
research records, two preserved unsupported-revision errors, four source-only
variants and fourteen full style-reference records. Historical profiles include
all supported editorial passes. The two generic production review/revision roles
have four new actual-body IDs across both formats; their text is identical.
Complete research records include operational/anchored/navigation instructions.
Twelve complete requests for six ordinary roles in both formats also match
`82c36d20`: normal manifest compilation, fresh preview and saved preview produce
identical full instructions, payloads, schemas, tools and metadata. The fixture
selects evidence deterministically; output fields are not normalized away.
No substring-only comparison or model response is used as the equality oracle.

The concurrent role-tool change intentionally removes the planning-selection
paragraph from nonplanning roles in eight records. It is excluded from the
baseline comparison above. The new `fact-verification.md` semantics belong to a
separate reviewed change; the prechange parity is retained in these raw snapshots.

## Package resources and consumers

| Resource group | Markdown files | Consumers |
| --- | ---:| --- |
| Active `current/` | 22 | Production manifest/fresh preview; thirteen roles, both formats, three source modes, repairs and style |
| Historical named releases | 37 | `existing/1`, `narrative-rebuild/1,/2`, `editorial-candidate/1,/2` |
| Frozen research/source adapters | 25 | Fingerprint-bound archived research experiments |
| Numbered components | 18 | Published `whole-episode-a/1,/2,/3` API |
| Frozen runtime continuation / legacy repair | 2 | Old manifests without the corresponding instruction pin |

Seven historical JSON catalogs reference those bodies: **89 runtime historical
resources total**. Supported release/adapter bodies are deduplicated by their
existing path references. Two identical numbered components remain at their
published v1/v2 component paths (solo and draft); no extra archive format was
introduced to remove those compatibility paths. Every retained body has one of
the consumers listed above. Unshipped/candidate samples are removed from runtime;
needed comparison samples are named fixtures under `backend/tests/fixtures/`.
Unreferenced opaque hash-named files are removed. Full old revisions remain in Git.

`research-continuation.md` also owns checkpoint/deferred-exchange and structured
material-return instructions. New research manifests pin that complete versioned
bundle once; old manifests use its exact frozen v1 text. The legacy repair suffix
is a named immutable resource with its original checksum, not prose in Python.

The offline wheel contains all 22 current Markdown and all 89 required historical
resources, with no `behavior/assets` tree or opaque hash-named Markdown. An isolated
installed-package check renders 32 active/historical assemblies independently of
the checkout. Unknown conditions, malformed/unclosed directives, duplicate named
sections, missing sections and include cycles fail explicitly, preserving bytes.

## Validation

**119 tests pass**, including the installed wheel, ordinary manifest/fresh/saved
preview, four installed-package restarts with current/history prompt resources
removed, exact historical request replay, research workflow, source-off preparation
after two current wording edits, history reopening on its next paid-response mock,
and repeated source-off replay without new calls. Hosted web_search and include
remain present on all three source-off turns. Search repeats and 9600-character
history hints match executing tools; no X report catalog is exposed in source-off.
The group used the shared heavy-test lock. One existing Alembic deprecation warning
remains. Lint and staged whitespace checks pass.

## Evidence and limits

[Compact proof](proof.json). Full snapshots, extraction/measurement scripts and
private test logs stay in
[`tmp/prompt-current-2026-11-22/`](../../../tmp/prompt-current-2026-11-22/):
[before](../../../tmp/prompt-current-2026-11-22/before.json),
[isolated after](../../../tmp/prompt-current-2026-11-22/after-isolated.json),
[parity](../../../tmp/prompt-current-2026-11-22/parity.json),
[wheel](../../../tmp/prompt-current-2026-11-22/wheel-proof.json),
[installed checks](../../../tmp/prompt-current-2026-11-22/installed-proof.json),
[acceptance log](../../../tmp/prompt-current-2026-11-22/tests-acceptance.log).

The earlier broad diagnostic group exposed stale unsafe-count-interruption test
fixtures and an independently corrected gateway binding bug. Its failures are
preserved in `tests.log` / `tests-core.log`, not counted as successful validation.
No claim of editorial acceptance or live model quality follows from text parity.

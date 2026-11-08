# Wright first flight — transfer input

Frozen input SHA-256: `f92ace1fc2ed82b2bea34565f5ada718792a7406862caf1fae8cd04989a850d8`.

The Russian request is only: «Расскажи, как братья Райт впервые поднялись в воздух.» One narrator, `РАССКАЗЧИК`; target 360 seconds, range 300–420. The audience hears the story for the first time. There is no supplied plan, required opening, final scene, number quota or prescribed story criterion.

The stock contains 12 factual cards and 19 exact source excerpts: control difficulties, helpers and photography, launch conditions, wind measurements, repeated trials and repairs, low-altitude decisions, damage on the ground, family communication and the participants’ public account. Four passages are explicitly selected for narrative wording; other cards retain their complete story facts. These are choices of available source wording, not a required sequence or coverage list.

## Source admission

Every `source_path` in `shared.json` is relative to **this directory**:

| Key | File / `source_path` | `purpose` |
|---|---|---|
| B1 | `brief.md` | `brief` |
| D1 | `dossier.md` | `dossier` |
| P1 | `sources/orville-diary-1903-12-17.txt` | `primary` |
| P2 | `sources/wright-statement-1904-01-05.txt` | `primary` |
| P3 | `sources/orville-telegram-1903-12-17.txt` | `primary` |

Admit these five files with ordinary `editorial-source-import`, one new scope such as `wright-transfer-2026-11-08`, and the paths above. Map those exact paths to their resulting refs in `source-map.json`. Then use ordinary `editorial-import --input INPUT_DIR/shared.json --source-map SOURCE_MAP --out SHARED_REF`. The case is `wright-first-flight`, `--plan-mode own-plan`; no fixed plan is provided. Import performs no model call.

## Authenticity, rights and representations

The three primary documents are by the participants, **not three independent witnesses**. Their English bodies are reproduced in the official NASA documentary collection *The Wind and Beyond*, NASA SP-2003-4409. Only Wright-authored historical text is admitted; NASA editorial essays are excluded. The full acquisition PDF, raw page extractions and inspected page renders are retained in `acquisition/`.

The Library of Congress finding aid states that copyright in the brothers’ unpublished writings in its custody was dedicated to the public. The diary and telegram are identified in the LOC collection; NPS reproduces the diary passage and telegram, and Smithsonian identifies the transmission discrepancy. Direct LOC pages returned 403, so no new visual inspection of original handwriting is claimed. The NASA reproductions at PDF pages 73 and 294–296 were inspected visually. Authors, dates, original URLs, rights authority and transformation details are in `provenance.json`.

Running headings were removed and consecutive document pages joined. Original spelling, punctuation, line breaks and line-end hyphenation remain. In particular, `57` in the telegram is retained; the diary and later statement say `59`. The January statement’s printed wording is not silently corrected. All quoted offsets refer to Unicode codepoints `[start,end)` in the delivered text representations.

`validation.json` records the published JSON Schema check, parser check, source SHA-256 checks and all 19 exact range matches. No paid calls or plot-specific evaluation criteria were created.

# GPS and John Snow development corpus

The [frozen batch](wp02-batch-01/) contains two Russian-language development episodes with a 15-minute target. GPS uses two speakers to explain how a receiver solves for its position and clock error. Snow uses one narrator to reconstruct an investigation in 1854 London. An editorial session produced the briefs, dossiers, reference plans, two full scripts per case and the accompanying notes. The batch has no independent professional or human audience rating.

This is the English operator guide for the batch. Its original Russian technical notes, source indexes and preparation scripts are part of the checksummed fixture. Changing even a Markdown heading would invalidate the [inventory](wp02-batch-01/inventory.sha256.json). Maintain operator documentation outside the frozen directory. The public copy removes transient HTTP cookies from the FAA and NASA download receipts and updates the two receipt entries in the outer inventory. The GPS source documents and their evidence bindings are intact.

## Verify and export

Run these commands from the repository root. Output directories must be new and outside the batch. Verification needs only the Python standard library.

```sh
python3 evals/cases/development/wp02-batch-01/tools/prepare.py verify
python3 evals/cases/development/wp02-batch-01/tools/prepare.py export-author \
  --case gps-clock --output tmp/gps-author
python3 evals/cases/development/wp02-batch-01/tools/prepare.py export-first-read \
  --case snow-pump --stage final --blind-id K7 --output tmp/snow-reader-K7
```

The verifier checks file membership and SHA-256, UTF-8 excerpt boundaries, control recipes and explicit calculations. The author export contains the selected brief, dossier, plan and that case's sources. The first-read export contains only its assigned spoken text and task. Use `--start` and `--stop` for an inclusive window of one-based utterance numbers. A fresh blind assessment requires a reader without access to the answer key or source directory. A directory name alone cannot enforce that isolation.

## Reading and import

| Role | Material and decision |
| --- | --- |
| Writer | Use the selected case under [author/](wp02-batch-01/author/) with its sources. Exclude reference scripts and judgments. |
| Initial reader | Read the assigned speech in order. Record where the explanation breaks, what the listener knows there and which exact lines caused the problem. Freeze those observations before opening the brief. |
| Fact-checker | Independently identify important claims, locate matching evidence and check its scope. Only then compare with the author's references. |
| Listener | Hear one assigned recording or passage, with no labels revealing version, expected interpretation or another listener's response. |
| Operator | Assign neutral IDs and permissions, record the data each role received and retain independent observations. Mark comparisons as contaminated when participants have seen the key. |

The production [staging and import guide](../../../backend/README.md) covers the database-backed path. Source originals retain their acquisition dates, bytes and rights. Text extractions and image renders are separately identified representations. Evidence ranges refer to Unicode code points in a particular UTF-8 text. Local G/S labels and repository paths are resolved to immutable revisions on import. Export a fresh source map outside the batch. A local ID or an editorial self-check cannot authorize publication.

The first revision is available in Git at `b449d4d`. A subsequent revision added a parish report and corrected recipe and evidence bindings. The Snow research and final script both changed, so comparing those versions cannot isolate the effect of rewriting alone. The original GPS and Snow drafts are preserved alongside revised candidates in [evaluator-only/](wp02-batch-01/evaluator-only/). Exact editions, warnings and relationships remain in the [frozen history](wp02-batch-01/CORPUS-HISTORY.md).

## Source provenance and rights

The [GPS corpus](wp02-batch-01/sources/gps/) includes NASA's 2017 introduction, the 2020 GPS Standard Positioning Service performance standard and the FAA overview. The NASA catalog records public distribution and government public use, the standard carries Distribution Statement A, and the FAA source is a federal-agency explainer. The original PDF supplies the equation on NASA slide 38, which its text layer omits. The inspected pages and the accompanying render are indexed with the evidence.

The [Snow corpus](wp02-batch-01/sources/snow/) separates the 1855 work, Project Gutenberg's transcription and its derived text. The [Gutenberg license](wp02-batch-01/sources/snow/gutenberg-license.html) and transcriber's notes accompany the book. The later parish report includes a Public Domain Mark, its acquisition record, OCR and selected page renders. The source index identifies inspected pages, transcription errors and the dependence between accounts. A saved original establishes which edition was available. Each reading claim still needs its own page or passage.

This provenance concerns the named source items. Preserve their individual attribution and notices when distributing copies. The [original source inventory](wp02-batch-01/sources/README.md) retains the recorded rights evidence and extraction details.

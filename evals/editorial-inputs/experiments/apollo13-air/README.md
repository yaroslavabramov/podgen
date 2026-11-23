# Apollo 13 and the cabin air supply

The [original brief](brief.md) commissions a seven-minute Russian documentary with one narrator and a six-to-eight-minute range. The hand-prepared [dossier](dossier.md) and [import package](shared.json) supply the evidence. Podgen selects the story and writes the plan and script.

## Sources

| Local text | Original and coverage |
| --- | --- |
| [Air-to-ground transcript](sources/air-to-ground.txt) | NASA Apollo Spacecraft Program Office, *Apollo 13 Technical Air-to-Ground Voice Transcription*, April 1970. Five continuous passages on printed pages 410, 411, 415, 448 and 455 (PDF pages 417, 418, 422, 455 and 462). Each has a tape number and mission elapsed time. [NASA transcript catalogue](https://www.nasa.gov/history/mission-transcripts-mercury-gemini-and-apollo/) · [Scanned transcript](https://www.nasa.gov/wp-content/uploads/2026/01/as13-tec.pdf) |
| [Mission report](sources/mission-report.txt) | *Apollo 13 Mission Report*, MSC-02680, September 1970. The start of the summary on page 1-1 and the canister paragraph in section 6.7 on page 6-10 cover instrument readings and a modification after twenty hours. [NASA NTRS record and scan](https://ntrs.nasa.gov/citations/19710003598) |
| [Ground test](sources/ground-test.txt) | J. C. LeBlanc, *Apollo 13 LiOH Canister Breakthrough Test*, July 15, 1970. Internal pages 3–5 (PDF pages 6–8) document the purpose, test rig, respiratory simulation, second test phase on April 16 and conclusions. [NASA NTRS record and scan](https://ntrs.nasa.gov/citations/19720021432) |
| [Commander's account](sources/lovell.txt) | James Lovell, chapter 13 of *Apollo Expeditions to the Moon*, NASA SP-350, 1975. The final paragraph of section 13.3 and the beginning of section 13.4 precede the discussion of navigation. The extract marks a sentence from NASA's photograph caption separately. [Chapter](https://www.nasa.gov/wp-content/uploads/static/history/SP-350/ch-13-1.html) · [Section 13.3](https://www.nasa.gov/wp-content/uploads/static/history/SP-350/ch-13-3.html) · [Section 13.4](https://www.nasa.gov/wp-content/uploads/static/history/SP-350/ch-13-4.html) |

The transcriber checked the first three documents against the scanned pages, joined words split across lines, normalized column spacing and rendered subscripts as digits. The transcript retains unfinished, repeated and inaudible words. Its timestamps count days, hours, minutes and seconds from launch. Lovell's account uses the NASA HTML text with line breaks joined. Bracketed page and passage markers were added for this corpus.

The package identifies each local text by SHA-256 and locates quotations with half-open character spans `[start, end)`. All four English extracts are ASCII, so these offsets also match UTF-8 byte positions.

### Original downloads

| Source as retrieved on November 10, 2026 | SHA-256 |
| --- | --- |
| Air-to-ground scan | `e295cfc36f66889da3d376dc3060e014c2ea6ac253927afb6d4408ef68d8917b` |
| Mission report scan | `f6941eaeabf8d611d3da88853923d7e85a8f578c577b02b7e65ba9d92bb54988` |
| Ground test scan | `f322f6de04c1523d6f0ec8dd91b406145b9b4af1c980778b8fe5ca992570b903` |
| Lovell, section 13.3 HTML | `83ecb450afc44a00d8e244faf255ecbd959f655d60e8087fe1048f10cf680882` |
| Lovell, section 13.4 HTML | `29ed8df4745ac982c4a8c7680b3a61929c8721fcad3b7e5e7c20c9bdcab5a4cc` |

## Import

From the repository root, use an existing project, authorized actor, database connection and object store. The importer validates the package and writes `shared-ref.json` for an own-plan run.

```sh
uv run --project backend python evals/editorial-inputs/current/snow-pump/import_case.py \
  --input evals/editorial-inputs/experiments/apollo13-air/shared.json \
  --project "$PROJECT_ID" --actor "$ACTOR_ID" --objects "$OBJECTS_DIR" \
  --scope apollo13-air --out-dir "$IMPORT_DIR"
```

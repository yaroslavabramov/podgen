# Why the first research stage uses a hosted service

Research design recorded on 13 November 2026. A new episode needs enough reporting to tell a story and enough original material to check the finished script. The design uses a hosted service for the initial investigation and keeps evidence, selection and editorial judgment inside Podgen. The first integration was Gemini Deep Research. An OpenAI Responses run with hosted web search was the proposed comparison if the first service proved insufficient. [Gemini Deep Research](https://ai.google.dev/gemini-api/docs/deep-research) · [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search)

## The workflow

1. Translate the EpisodeSpec into a research assignment, specifying the audience, time and source scope, user question and report language.
2. Submit one authorized hosted job. Retain the exact request, provider configuration, visible events, returned report and links.
3. Acquire the linked documents and store the actual bytes with acquisition time and provenance. Extract text from those retained originals.
4. Expose the whole report in navigable sections and give the planner and its editor access to the retained material. Keep their selections and readings.
5. Send the accepted plan and selected material to the writer. Verify claims in the actual written script against retained source passages. Request specific additional material when needed.
6. Pass an approved, exact script version to audio. The research service cannot choose the episode's scenes, determine final acceptance or authorize synthesis.

[Hosted job implementation](2026-11-13-hosted-research-integration.md) · [Research materials](../research-materials-runtime.md) · [Corpus reading](../research-corpus-runtime.md)

## What research should produce

For a historical topic, ask for people, decisions, obstacles, events, consequences and scale. For a technical topic, ask for the relevant mechanism, prerequisites and a concrete example. Keep a meaningful disagreement or an uncertainty next to the claim it affects. Make the research document readable enough that a planner can distinguish a promising line of inquiry from a list of facts. The original design included the following assignment template. The installed profile owns the exact request sent by a particular run.

```text
Research the assignment below as source material for a podcast.

Write your report in output_language. Preserve quotations in their original
language and provide source links next to the claims they support.

Explain the main question and the background a non-specialist needs to
understand it. Follow the relevant mechanisms or sequence of events. Retain
documented people, decisions, obstacles, consequences, scale and concrete
examples when they contribute to the assignment.

Read primary documents where they are useful and compare them with strong
secondary explanations. Distinguish evidence from later interpretation.
Check the scope, dates, units and denominators of important numerical claims.
Follow material disagreements without inventing a controversy.

Do not prescribe the episode's opening, acts or ending. Produce readable
research material, not dialogue or a podcast script. Do not turn every finding
into an isolated fact card.

State unresolved issues when they change the answer. Distinguish an
inaccessible source or incomplete search from uncertainty in the subject.
Keep meaningful limitations with the relevant explanation.

Finish with a source list identifying the most useful documents and what
each contributes. Include short original passages for central conclusions
when available, so they can be located in the source later.
```

## Receiving the provider result

Assign an immutable identity to the exact assignment, provider configuration and prepared request. Preserve the provider job ID, status, reported usage, unabridged response, parsed report, visible events and reported links. This outline describes the application record independently of the provider's response schema.

```text
HostedResearchResult:
  assignment_ref
  provider_profile_ref
  provider_job_id
  request_ref
  raw_response_blob
  report_text_blob
  reported_sources[]
  visible_event_refs[]
  usage
  status
```

An interrupted run with a known job should continue that job. If submission is uncertain, surface that state before a new charge is possible. A URL in the report is a lead. Its occurrence, an acquired revision and a passage actually read by a reviewer are separate records.

## Reusing acquisition work

An earlier implementation already handled HTML, PDF, rendered snapshots, error pages, safety checks, timeouts, interrupted work and concurrent acquisition. A historical adapter test run passed 113 cases in about 46 seconds with substituted dependencies. That result justified porting the useful extraction logic. It did not establish success against every live site or on a new OCR input.

The port needed five changes. Retain original bytes and acquisition metadata. Bind extraction to the exact saved response. Enforce limits during streaming. Keep fallback representations distinct from the HTTP original. Publish through the current repository, object storage and revision contracts. A second HTTP request can return a different document, even at the same URL. When technical quality is low, record what was actually retrieved. [Acquisition runtime](../source-acquisition-runtime.md)

## Planning and follow-up

Make the entire report available and give the planner a usable overview, source catalog, section reads, surrounding context and pagination. Log which ranges were read. Retain original report and source references beside decisions. The writer needs the selected reporting, and the fact reviewer needs original passages for the claims actually present in the script.

| Missing material | Appropriate next operation |
| --- | --- |
| A nearby paragraph, definition or table already in the corpus | Open or search the saved document. |
| An explanation of a point in the existing report | Read the relevant section or commission a bounded follow-up on those materials. |
| New documents or a material unresolved question | Prepare a focused hosted assignment with the passage, relevant context and question. |
| An inaccessible document or PDF | Use the acquisition and extraction path and record the outcome. |

A provider conversation follow-up may use an ordinary model. If the assignment needs a fresh investigation, identify the research mode explicitly and inspect the resulting work. Consider a dedicated local research agent when a defined requirement remains unmet, such as research over a private corpus.

## Qualification sequence

The original plan called for a single live report about navigation and longitude before integrating the planner. Read the full report and its source links. Review whether it explains the question, contains substantive usable material and gives enough information to locate supporting documents. Then port acquisition with its tests, expose the real report to planning, support targeted follow-ups and evaluate a complete new episode. Compare the resulting plan and its separate review, first and final scripts and unresolved evidence gaps. Individual passing integrations establish technical behavior at their own boundary.

# Podgen Generation V2: Editorial Agent Architecture

Date: 2026-06-01
Status: proposal
Scope: replacement architecture for generation quality, not a local fix to the current claim-first pipeline

## Why V2 Exists

The current pipeline is strong at preservation of evidence lineage, retries, debug dumps, and safety gates. Its main weakness is that the creative process is shaped by claim accounting too early:

```text
corpus -> evidence -> claims -> act claim allocation -> scene claim allocation -> script
```

That flow makes the model behave like an allocator of factual atoms. It can be made safer, but it naturally produces sterile plans, repeated explanatory moves, and local validator conflicts. The system asks for long-form editorial quality while forcing each scene to justify itself as a claim-bearing unit.

V2 changes the primary intermediate representation:

```text
corpus -> source cards -> editorial atoms -> research memo -> treatment -> act dossiers -> act drafts
                                  \                                      \
                                   -> risk register                       -> citation/fact pass
```

Facts remain source-backed. The change is that facts stop being the main creative building block. The main creative building block becomes an editorial understanding of the material: conflict, mechanism, story, turn, cost, exception, consequence, unresolved tension.

## Design Principles

### 1. LLMs Are Capable Agents, Not JSON Fillers

Modern models can search, compare sources, notice editorial opportunity, and revise a plan. V2 should give them real agency inside bounded workspaces:

- let them choose which corpus tools to call
- let them form hypotheses and test them
- let them omit weak material
- let them write/rewrite larger narrative blocks
- require them to persist auditable artifacts after each phase

The model should not be reduced to one prompt per microscopic slot.

### 2. Deterministic Infrastructure Still Owns Truth, Identity, and Safety

The LLM decides what to investigate and how to structure the story. The system owns:

- source identity
- file ingestion
- exact locators
- access control
- cost budgets
- tool permissions
- artifact versioning
- citation/fact verification
- final audio production

### 3. Editorial First, Citations After Drafting

V1 tries to build the script from verified claims. V2 should first produce the best source-backed editorial draft, then extract/check/link factual assertions from that draft.

This is closer to how strong documentary writing works:

1. Understand the material.
2. Find the story.
3. Draft a compelling argument or narrative.
4. Fact-check every line that needs support.
5. Rewrite unsupported or overstrong lines.

### 4. Every Editorial Move Must Be Anchorable

Editorial material is not free invention. Each important atom carries anchors:

- direct quote anchors when possible
- source-level anchors when a source supports the idea broadly
- stitched anchors when the insight comes from combining several facts
- an explicit `interpretive` confidence label when the atom is editorial synthesis

### 5. Long-Form Requires Global Memory

For 60-90 minute episodes, local scene prompts are not enough. V2 needs durable memory artifacts:

- what has already been explained
- which rhetorical moves were used
- which open loops remain
- which terms are introduced
- which examples are reserved for later
- which phrases/patterns are banned because they have already appeared

### 6. Provider Capabilities Should Be Abstracted

OpenAI and other providers are moving quickly. V2 should not hardcode one model's feature set into product semantics. It should use a capability layer:

- structured outputs
- function/tool calling
- hosted file search
- remote MCP tools
- web search
- large context
- reasoning effort
- stateful response continuation
- trace support

OpenAI's current Responses API supports stateful interactions and tools such as file search, web search, and function calling in one interface. Official docs also describe hosted File Search/vector stores, strict function calling, remote MCP/connectors, Structured Outputs, web search, and the Agents SDK for handoffs/traces:

- [Responses API reference](https://platform.openai.com/docs/api-reference/responses)
- [File Search tool](https://platform.openai.com/docs/guides/tools-file-search)
- [Function calling](https://platform.openai.com/docs/guides/function-calling?api-mode=responses)
- [Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs?api-mode=responses)
- [Connectors and MCP servers](https://platform.openai.com/docs/guides/tools-connectors-mcp)
- [Web search tool](https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses)
- [Agents SDK](https://platform.openai.com/docs/guides/agents-sdk/)

V2 should use these capabilities where they help, while keeping local artifacts and source locators as the product source of truth.

### 7. Native Source Understanding Should Be First-Class

Deterministic extraction is not always the best representation of a source. This applies to PDFs and to web pages/articles:

- layout carries meaning
- tables, captions, sidebars, footnotes, and page structure can be damaged by text extraction
- OCR/Markdown conversion can add boilerplate, broken order, duplicate headers, and other noise
- article extraction can drop comments, captions, interactive sections, footnotes, charts, author/date metadata, or update notices
- rendered web pages can contain important context that trafilatura/Jina/httpx cleanup discards or reorders
- modern file-aware, browser-aware, and web-search-enabled models may understand the original source better than a lossy parse

V2 should treat deterministic ingestion as infrastructure, not as the only way the model may read. For each source, the workspace should expose multiple views:

- original file, when provider-native file upload/file search can use it
- original URL and fetched HTML snapshot for web sources
- rendered-page/browser snapshot for web sources where layout or dynamic content matters
- normalized extracted text, for local search and fallback
- chunks with locators, for deterministic provenance
- page images or rendered pages, when layout matters
- source cards and quote cards, for compressed memory

The model should be allowed to choose the best view for the task. For example:

- use the original PDF/file-search view to understand a report's structure
- use the original URL or rendered browser snapshot to understand a web article's visible context
- use provider web search for discovery/currentness, then local snapshots for stable provenance
- use rendered page images for visual tables or odd layouts
- use local chunks for fast targeted retrieval
- use deterministic text only as a fallback when native file reading is unavailable or too expensive

## High-Level Architecture

```text
                           ┌────────────────────────────┐
                           │  Source Workspace           │
                           │ originals + extracted views │
                           └──────────────┬─────────────┘
                                          │
                              ┌───────────▼───────────┐
                              │ Corpus Workspace V2    │
                              │ native files + indexes │
                              │ optional vector stores │
                              └───────────┬───────────┘
                                          │
            ┌─────────────────────────────▼─────────────────────────────┐
            │ Editorial Research Agent                                  │
            │ tool-using, hypothesis-driven, source-backed exploration  │
            └─────────────────────────────┬─────────────────────────────┘
                                          │
                      ┌───────────────────▼───────────────────┐
                      │ Source Cards + Editorial Atoms          │
                      │ conflict/mechanism/turn/cost/etc.       │
                      └───────────────────┬───────────────────┘
                                          │
                         ┌────────────────▼────────────────┐
                         │ Research Memo + Risk Register    │
                         └────────────────┬────────────────┘
                                          │
                          ┌───────────────▼───────────────┐
                          │ Story Editor / Treatment Agent │
                          └───────────────┬───────────────┘
                                          │
                             ┌────────────▼────────────┐
                             │ Episode Treatment        │
                             │ act map + omission logic │
                             └────────────┬────────────┘
                                          │
                 ┌────────────────────────▼────────────────────────┐
                 │ Act Dossier Compiler                             │
                 │ curated atoms, source cards, quotes, prior memory │
                 └────────────────────────┬────────────────────────┘
                                          │
                        ┌─────────────────▼─────────────────┐
                        │ Act Writer                         │
                        │ writes one full act, not one scene  │
                        └─────────────────┬─────────────────┘
                                          │
                 ┌────────────────────────▼────────────────────────┐
                 │ Continuity Ledger + Whole-Episode Critic         │
                 │ repetition, comprehension, structure, voice       │
                 └────────────────────────┬────────────────────────┘
                                          │
                           ┌──────────────▼──────────────┐
                           │ Rewriter                     │
                           │ block/act-level rewrites     │
                           └──────────────┬──────────────┘
                                          │
                    ┌─────────────────────▼─────────────────────┐
                    │ Draft Claim Extraction + Citation Linker    │
                    │ verify facts, repair unsupported lines      │
                    └─────────────────────┬─────────────────────┘
                                          │
                               ┌──────────▼──────────┐
                               │ TTS + shownotes      │
                               └─────────────────────┘
```

## Core Artifacts

### Corpus Workspace

The workspace is the model-accessible view of a project corpus. It is not just a vector DB.

It contains:

- canonical sources
- raw files
- normalized text
- chunks with locators
- embeddings
- source metadata
- source cards
- quote cards
- editorial atoms
- tool-call traces
- optional OpenAI vector store IDs

For large corpora, the workspace has tiers:

1. `Corpus Map`: compact overview of all sources.
2. `Source Cards`: one useful editorial summary per source.
3. `Thematic Packs`: clusters of source cards and atoms around a topic.
4. `Quote Cards`: selected quote-level anchors.
5. `Raw Read Tools`: exact source/chunk reading on demand.

The model should usually start from tiers 1-3 and only drill into raw text when it needs evidence, color, or disambiguation.

### Source Card

A source card says what a source is useful for, not just what it contains.

```json
{
  "source_id": "uuid",
  "title": "string",
  "source_type": "pdf|web|video|audio|book|paper|official|other",
  "authority": "primary|official|expert|secondary|tertiary|weak",
  "coverage": ["history", "mechanism", "statistics"],
  "best_uses": [
    "explains the 1983 currency board mechanism",
    "gives concrete denominations and issuing banks"
  ],
  "limitations": [
    "tertiary summary, not ideal for contested claims"
  ],
  "key_quotes": [
    {
      "quote": "short excerpt",
      "locator": "page/section/url/timestamp",
      "why_useful": "mechanism anchor"
    }
  ],
  "related_source_ids": ["uuid"]
}
```

### Editorial Atom

An editorial atom is the new central unit. It is source-backed, but it is not the same as a claim.

```json
{
  "atom_id": "uuid",
  "type": "conflict|mechanism|turning_point|exception|cost|consequence|character|scene|open_question|analogy|definition",
  "summary": "Private banks print official Hong Kong banknotes, but the system is not private monetary sovereignty.",
  "why_it_matters": "This is the episode's central paradox.",
  "listener_function": "hook|orientation|explanation|complication|payoff|transition",
  "source_anchors": [
    {
      "source_id": "uuid",
      "locator": "section/page/url/timestamp",
      "quote": "short excerpt or null",
      "support_type": "direct|contextual|illustrative"
    }
  ],
  "confidence": "direct|stitched|interpretive",
  "risk": "low|medium|high",
  "risk_reason": "Requires combining current issuing-bank facts with legal-control sources."
}
```

Atom types should be editorially useful:

- `conflict`: real tension or apparent contradiction
- `mechanism`: how the system works
- `turning_point`: event after which the story changes
- `exception`: thing that breaks a simple explanation
- `cost`: tradeoff, constraint, price, sacrifice
- `consequence`: what follows if the mechanism is true
- `character`: institution/person/group with agency
- `scene`: concrete event or image that can carry audio
- `open_question`: question the episode can hold and later answer
- `analogy`: explanatory analogy grounded in the topic
- `definition`: term that must be introduced

### Research Memo

The research memo is the output of the research phase. It is written for the story editor and writer.

It includes:

- central question
- short answer
- strongest editorial spine
- candidate act arcs
- strongest source cards
- editorial atoms
- weak or disputed areas
- omissions
- source risk register
- suggested next searches if the memo is not ready

The memo should not be a dump of every claim. It should be a compact, opinionated brief.

### Treatment

The treatment is the episode plan. It is not a beat sheet.

```json
{
  "episode_title_working": "string",
  "central_question": "string",
  "listener_transformation": {
    "starts_thinking": "string",
    "ends_understanding": "string"
  },
  "thesis": "string",
  "narrative_spine": "string",
  "act_map": [
    {
      "act_number": 1,
      "duration_target_minutes": 8,
      "act_question": "string",
      "editorial_job": "hook|origin|mechanism|complication|stress_test|payoff",
      "movement": "what changes for the listener",
      "required_atoms": ["atom_id"],
      "reserved_atoms": ["atom_id"],
      "must_not_do": ["do not explain the full mechanism yet"]
    }
  ],
  "omission_policy": [
    "Do not include minor denomination history unless it clarifies the main paradox."
  ],
  "repetition_policy": [
    "Do not reopen 'private banks print money' as a fresh surprise after Act 1."
  ]
}
```

### Act Dossier

The act dossier is the writer payload. It is curated and compact.

It contains:

- treatment slice for the act
- relevant editorial atoms
- best quote cards
- source cards for context
- already-covered ledger
- open loops to preserve
- terms already introduced
- rhetorical moves already used
- style/voice constraints
- fact risks for the act
- target word count and pacing

The writer should not receive the whole corpus by default. If it needs more, it can request specific reads through controlled tools, but writing should be the main job.

### Continuity and Repetition Ledger

The ledger is updated after every act draft and rewrite.

```json
{
  "covered_atoms": ["atom_id"],
  "introduced_terms": [
    {"term": "currency board", "act": 4, "definition_quality": "clear"}
  ],
  "open_loops": [
    {"question": "Why did the state keep private issuance?", "status": "open_until_act_3"}
  ],
  "used_openers": ["concrete object in hand", "historical counterfactual"],
  "used_rhetorical_moves": [
    "B asks naive possibility -> A narrows it",
    "apparent contradiction -> institutional explanation"
  ],
  "banned_repetitions": [
    "do not say again that this is not a quirky design detail",
    "do not reopen whether banknotes are official"
  ],
  "voice_notes": [
    "Host B has started three acts with confusion; next act needs hypothesis or challenge."
  ]
}
```

## Step-by-Step V2 Flow

## Step 0. Source Workspace and Corpus Build

Inputs:

- uploaded PDFs, URLs, videos, audio
- user topic/prompt
- language
- duration target
- show bible

Build a source workspace with several representations, not one canonical parse:

- original source file or original fetched artifact
- original URL, fetched HTML, and optional rendered snapshot for web sources
- provider-uploaded file IDs where supported
- optional hosted vector-store IDs where supported
- deterministic extraction, normalization, chunking, and embeddings
- rendered page images for PDFs where layout may matter
- extracted article text for web sources, treated as one view rather than canonical truth
- canonical source metadata
- raw storage and lifecycle tracking

Add V2 workspace creation:

- upload original files or stable fetched artifacts, not only normalized text, to optional hosted file/vector-store systems when provider supports it
- upload normalized text only as a fallback or as a second retrieval view
- keep local Qdrant/Postgres/MinIO as source of truth
- attach source IDs and locators to all model-visible snippets
- create an initial corpus map

OpenAI-specific option:

- use File Search/vector stores for broad semantic retrieval over uploaded original files
- use uploaded files directly when a model/tool path can inspect the file better than local Markdown extraction
- use Web Search for discovery, freshness, and corroboration, then snapshot/read chosen URLs into the local source workspace
- use remote MCP/connectors where a source system exposes a better canonical read API than generic HTML scraping
- use local tools for exact locator reads and canonical source identity

Reason:

Hosted file search, provider-native file understanding, web search, and browser/MCP-style source access are useful because models can decide when to retrieve from the representation closest to the source. For PDFs this may outperform local parsing; for web pages it may outperform article extraction when the page's visible context, update metadata, tables, or navigation structure matter. These tools should not replace local provenance because product citations need exact source IDs, locators, snapshots, and lifecycle control.

Outputs:

- `corpus_workspace_id`
- optional `provider_file_ids`
- optional `provider_vector_store_id`
- corpus map

Quality gate:

- every source has a source workspace status
- every source has source metadata
- every source has at least one usable model-readable representation
- every deterministic searchable chunk has a locator
- large files are readable by native file search, local retrieval, or both without relying on one context window

Important:

Do not assume deterministic extraction is superior. The source workspace should record which representation worked best:

- `native_file_preferred`
- `native_web_preferred`
- `deterministic_text_preferred`
- `rendered_pages_required`
- `browser_snapshot_required`
- `audio_transcript_required`
- `web_extraction_required`
- `hybrid_required`

This lets later agents choose the least lossy view instead of inheriting parser noise.

## Step 1. Corpus Triage Agent

Goal:

Build a useful map of a large corpus before deep research.

Tools:

- `corpus.list_sources`
- `corpus.search`
- `corpus.read_chunk`
- `corpus.sample_source`
- optional hosted `file_search`
- `source_card.create`

Behavior:

1. Read corpus map and source metadata.
2. Sample high-relevance and high-authority sources.
3. Identify duplicate or near-duplicate sources.
4. Classify each source by editorial utility.
5. Create source cards.

Outputs:

- `SourceCard[]`
- `source_clusters`
- `coverage_map`
- `source_quality_report`

Large corpus strategy:

- do not summarize every source with the same budget
- rank by authority, relevance, uniqueness, and source diversity
- create lightweight cards for low-value sources
- create rich cards for high-value sources
- keep source clusters so the research agent can ask for "best sources on mechanism" rather than searching the whole corpus repeatedly

Failure mode to avoid:

- flat corpus summaries that say "this source discusses X" without explaining what the source is useful for.

## Step 2. Editorial Research Agent

Goal:

Let an LLM investigate the corpus like a producer/researcher, not like a claim extractor.

Inputs:

- user prompt
- show bible
- duration target
- corpus map
- source cards
- budget

Tools:

- `corpus.search(query, filters, purpose)`
- `corpus.read(anchor_or_source, window)`
- `corpus.compare_sources(source_ids, question)`
- `corpus.find_quotes(topic_or_atom)`
- `web.search(query, domains, recency, purpose)`
- `web.read(url)`
- `note.save`
- `editorial_atom.create`
- `gap.mark`
- `budget.status`
- `finish_research_memo`

Agent behavior:

1. Form initial hypotheses:
   - what is the likely story?
   - what is the central tension?
   - what mechanisms matter?
   - what would make this worth 60-90 minutes?
2. Search the corpus to test those hypotheses.
3. Create editorial atoms only when source anchors support them.
4. Mark weak areas as gaps rather than inventing connective tissue.
5. Use web search only when corpus coverage is weak, stale, or needs independent corroboration.
6. Produce a research memo with an explicit risk register.

Important:

The agent is allowed to think strategically. It should not be forced to cover every branch or every source. It should be judged on whether it found the strongest story supported by the corpus.

Outputs:

- `EditorialAtom[]`
- `ResearchMemo`
- `RiskRegister`
- `ResearchGap[]`
- `ToolTrace`

Quality gate:

- every major atom has anchors
- every `interpretive` atom has a clear basis and risk label
- no high-risk atom is allowed into treatment without either corroboration or hedging
- the memo names what it deliberately omitted

## Step 3. Research Auditor

Goal:

Separate "compelling synthesis" from unsupported overreach before story planning.

Inputs:

- research memo
- editorial atoms
- source cards
- risk register
- selected raw anchors

Tools:

- `corpus.read`
- `corpus.search`
- `web.search`
- `atom.update_risk`
- `gap.mark`

Behavior:

- challenge the central thesis
- sample-check anchors
- identify atoms whose `why_it_matters` outruns evidence
- ask for targeted follow-up only where it matters
- approve, approve with cautions, or send back to research

Outputs:

- `ResearchAudit`
- updated risks/gaps
- approved atom set

Quality gate:

- no high-impact unsupported synthesis moves forward silently

## Step 4. Story Editor / Treatment Agent

Goal:

Build the episode's editorial architecture from the memo and atoms.

Inputs:

- research memo
- approved editorial atoms
- source cards
- show bible
- duration target
- audience level
- language

Tools:

- `atom.inspect`
- `source_card.inspect`
- `treatment.save`

Behavior:

1. Decide the central question.
2. Decide the listener transformation.
3. Decide the narrative spine.
4. Choose act count and act functions based on duration.
5. Assign atoms to acts as narrative material, not as claim quotas.
6. Write omission policy.
7. Write repetition policy.
8. Reserve strong examples for the right moment.

Outputs:

- `EpisodeTreatment`

Quality gate:

- treatment has a clear beginning/middle/end
- each act changes the listener's understanding
- every act has a job
- material is omitted intentionally
- no act exists only to use leftover facts

Duration guidance:

- 15-30 min: 3-5 acts
- 45-60 min: 5-7 acts
- 75-90 min: 6-9 acts

The exact count should follow the material. A 90-minute episode with weak source depth should not be padded.

## Step 5. Act Dossier Compiler

Goal:

Prepare a writer-ready packet for one act.

Inputs:

- episode treatment
- act slice
- approved atoms
- source cards
- quote cards
- continuity ledger
- risk register

Tools:

- `corpus.find_quotes`
- `corpus.read`
- `dossier.save`

Behavior:

- gather only the material needed for this act
- include enough context for continuity
- include the best concrete details and quotes
- state what not to repeat
- state what must remain open
- state fact risks and hedging requirements

Output:

- `ActDossier`

Quality gate:

- writer can draft without searching the entire corpus
- dossier has concrete material, not just abstractions
- act-specific risks are visible

## Step 6. Act Writer

Goal:

Write a full act as a coherent spoken segment.

Inputs:

- act dossier
- prior act summaries
- continuity ledger
- show bible
- target duration/word count

Tools:

- optionally `corpus.read` for targeted clarification
- `draft.save`

Behavior:

- write the act as a whole
- preserve open loops and payoffs
- avoid repeating prior rhetorical moves
- use host dynamics as conversation, not as Q/A scaffolding
- write for audio comprehension
- do not force every paragraph to carry a new fact

Outputs:

- `ActDraft`
- `ActSelfReport`

Important:

The writer should not be allowed to use broad search tools by default. If the writer keeps searching, the dossier is weak or the writer is doing the researcher's job.

Quality gate:

- full act has internal movement
- target length is within tolerance
- no obvious repeated opener/teacherly pattern
- no ungrounded factual confidence on known risk areas

## Step 7. Continuity Ledger Update

Goal:

Make the next act aware of the real drafted episode, not just the plan.

Inputs:

- act draft
- previous ledger
- treatment

Tools:

- `ledger.update`

Behavior:

- summarize what the listener now understands
- mark atoms covered
- mark terms introduced
- mark phrases and rhetorical moves used
- update open loops
- identify repeated patterns to avoid next

Output:

- updated `ContinuityLedger`

Quality gate:

- next act receives a compact but concrete memory of actual written text

## Step 8. Whole-Episode Critic

Goal:

Evaluate the draft at the level where long-form quality actually lives.

Inputs:

- full draft or all act drafts
- treatment
- research memo
- continuity ledger

Tools:

- `critique.save`

Rubric:

- central question remains alive
- act sequence has escalation
- listener comprehension is maintained
- no repeated explanation pattern dominates
- host dynamics evolve
- transitions are earned
- facts are not overclaimed
- ending pays off the beginning

Outputs:

- `EpisodeCritique`
- targeted rewrite requests

Quality gate:

- if the problem is structural, do not line-polish
- rewrite the act/block whose job is wrong

## Step 9. Rewriter

Goal:

Rewrite at the right granularity.

Inputs:

- act/block draft
- rewrite request
- treatment
- relevant dossier
- continuity ledger

Tools:

- `draft.update`
- optionally `corpus.read` for narrowly scoped fact clarification

Rewrite modes:

- `line_repair`: factual wording only
- `block_rewrite`: one section of an act
- `act_rewrite`: full act
- `transition_rewrite`: boundary between acts
- `global_rebalance`: treatment-level adjustment, then affected act rewrites

Quality gate:

- acceptance checks against the targeted critique, not generic style improvement

## Step 10. Draft Claim Extraction

Goal:

Extract factual assertions from the actual draft.

This replaces the V1 assumption that claims must be created before writing.

Inputs:

- final-ish draft
- research memo
- source cards
- atom anchors

Tools:

- `claim.extract`
- `corpus.search`
- `corpus.read`

Outputs:

- `DraftClaim[]`

Draft claim schema:

```json
{
  "claim_id": "uuid",
  "text_span": {
    "act": 2,
    "start_char": 1020,
    "end_char": 1174
  },
  "statement": "minimal factual proposition",
  "claim_type": "fact|interpretation|opinion|reported_claim",
  "confidence_required": "verified|observed|attributed|hedged",
  "source_need": "direct|contextual|none_generic",
  "risk": "low|medium|high"
}
```

Quality gate:

- extracted claims must be minimal
- interpretive synthesis is labelled as such
- generic conversation lines are not over-extracted

## Step 11. Citation Linker and Fact Verifier

Goal:

Link draft claims to source anchors and verify whether the spoken wording is allowed.

Inputs:

- `DraftClaim[]`
- editorial atoms
- quote cards
- source cards
- corpus tools

Tools:

- `corpus.search`
- `corpus.read`
- `citation.link`
- `claim.mark_verified`
- optional web search for independent corroboration

Outputs:

- `CitationLink[]`
- `FactIssue[]`

Issue types:

- `unsupported_fact`
- `overstrong_wording`
- `missing_attribution`
- `source_mismatch`
- `stale_or_date_sensitive`
- `interpretation_needs_hedge`
- `quote_not_verbatim`
- `citation_locator_missing`

Quality gate:

- no blocking fact issues before TTS
- high-risk interpretive lines must be hedged or rewritten
- every citation link has source ID and locator

## Step 12. Fact Repair

Goal:

Fix unsupported lines while preserving the act's editorial shape.

Inputs:

- draft
- fact issues
- citation links
- original act dossier

Repair hierarchy:

1. add attribution
2. hedge wording
3. narrow the claim
4. replace with source-backed adjacent fact
5. remove line only if it is not structurally needed
6. rewrite block if the unsupported line carries the block's logic

Quality gate:

- repaired text still sounds like the same episode
- no new factual claims are introduced without link/audit

## Step 13. Audio and Shownotes

Existing TTS and shownotes can remain mostly as-is, with one difference:

- shownotes should be generated from final citation links and source cards, not from pre-draft claims alone.

## Tool and MCP Design

V2 should expose corpus capabilities through a controlled tool layer. MCP is a good fit because it lets a model discover and call tools through a standard protocol. OpenAI's Responses API can use remote MCP servers/connectors, while local agents can use the same server directly.

The important point is not "raw DB access". The important point is "broad enough access through safe tools".

### Read Tools

```text
corpus.list_sources(project_id, filters)
corpus.search(project_id, query, filters, top_k, purpose)
corpus.read_source(source_id, locator?, max_chars)
corpus.read_chunk(chunk_id, surrounding_chunks)
corpus.get_source_card(source_id)
corpus.get_quote_card(quote_card_id)
corpus.compare_sources(source_ids, question)
```

### Write Artifact Tools

```text
source_card.create/update
editorial_atom.create/update
research_memo.save
treatment.save
act_dossier.save
draft.save/update
ledger.update
claim.extract/save
citation.link/save
issue.create
```

These are append/versioned writes, not arbitrary mutation.

### External Research Tools

```text
web.search(query, filters, allowed_domains, recency, purpose)
web.read(url)
web.save_source_candidate(url, reason)
```

Web tools should be budgeted and traced. They are for gaps, updates, and corroboration, not for replacing the uploaded corpus.

### Budget and Control Tools

```text
budget.status()
budget.request_more(reason)
phase.finish(summary, artifacts)
phase.block(reason, missing_inputs)
```

The model should know the budget and finish intentionally.

## Large Corpus Strategy

### Problem

A large corpus cannot be handled by "put everything in context". Even if a model supports a very large context window, quality degrades when the prompt becomes a warehouse.

### Strategy

Use layered memory:

1. `Corpus Map`: breadth.
2. `Source Cards`: source-level utility.
3. `Editorial Atoms`: story-useful facts/synthesis.
4. `Quote Cards`: selected exact anchors.
5. `Raw Read`: exact pull on demand.

### Retrieval Mix

Use multiple retrieval modes:

- local vector search for product-owned chunks
- keyword search for names/dates/terms
- source-card search for editorial utility
- hosted file search for model-managed exploratory retrieval
- direct source reads for exact verification

No single retrieval mode should own truth.

### Native Source vs Deterministic Parse

For PDFs, web articles, and other complex sources, V2 should actively compare representations:

- If the deterministic parse is clean, cheap, and locator-preserving, use it for most local search.
- If the parse is noisy, layout-damaged, table-heavy, or stripped of important web context, prefer the original uploaded file, original URL, rendered browser snapshot, provider web search result, or rendered pages for understanding.
- If the model finds something through hosted file search, resolve it back to a local source/page/locator before final citation.
- If the model finds something through web search, snapshot the URL and resolve the claim to a stable local source record before final citation.
- If exact page/line provenance cannot be recovered from provider-native retrieval, run a targeted local read/OCR/page render pass only for that cited region.
- If article extraction and browser-visible content disagree, prefer the browser-visible/source-canonical representation and record the extractor discrepancy.

This flips the V1 assumption. The deterministic parser is not the authority on what the source means. It is a provenance, search, and fallback tool. The original source or canonical source API remains the authority.

### Corpus Sharding

For very large projects:

- create source clusters by topic/time/entity
- create vector stores by cluster if provider limits or cost require it
- maintain a global source-card index
- let the research agent ask for a cluster pack
- only load raw source text for contested or high-value material

### Memory Compression

Do not compress raw evidence into generic summaries and throw away anchors. Compression artifacts must preserve:

- source ID
- locator
- quote or exact paraphrase boundary
- confidence
- limitation
- why useful

## Long Podcast Design: 60-90 Minutes

V2 is especially suited for long podcasts if it is hierarchical.

Approximate spoken-script sizes:

- 30 minutes: 4,000-5,000 words
- 60 minutes: 8,000-10,000 words
- 90 minutes: 12,000-15,000 words

Do not generate a 90-minute script in one call. Generate:

1. research memo
2. treatment
3. act dossiers
4. act drafts
5. whole-episode critique
6. targeted rewrites

For 60-90 minutes, recommended structure:

- 5-9 acts
- each act 7-14 minutes
- each act written as a whole
- no scene-level claim quota
- ledger updated after every act
- whole-episode critique before citation repair

Long-form quality comes from:

- escalation
- controlled withholding
- varied rhetorical moves
- concrete examples
- structural memory
- omission discipline

It does not come from adding more claims.

## Provider Capability Layer

Create an internal `ModelRuntime` abstraction:

```python
class ModelRuntimeCapabilities:
    structured_outputs: bool
    function_tools: bool
    parallel_tool_calls: bool
    hosted_file_search: bool
    hosted_web_search: bool
    remote_mcp: bool
    stateful_responses: bool
    reasoning_effort: bool
    trace_export: bool
    max_context_tokens: int
```

For OpenAI:

- use Responses API for stateful tool-using phases
- use Structured Outputs for final artifacts
- use function calling for local product tools
- use remote MCP for corpus tools when useful
- use File Search/vector stores for broad retrieval over uploaded/normalized files
- use Web Search for latest/corroborating sources where policy allows
- use Agents SDK concepts for handoffs/tracing if it fits the implementation

For other providers:

- map local tools to their tool-calling API
- fall back to local retrieval if no hosted file search exists
- preserve artifact schemas and quality gates

The product should depend on artifact contracts, not on one provider's exact tool syntax.

## Storage and Runtime Proposal

Add V2 tables or JSONB artifacts without disturbing V1:

- `generation_v2_runs`
- `corpus_workspaces`
- `source_cards`
- `quote_cards`
- `editorial_atoms`
- `research_memos`
- `research_audits`
- `episode_treatments`
- `act_dossiers`
- `act_drafts`
- `continuity_ledgers`
- `episode_critiques`
- `draft_claims`
- `citation_links`
- `fact_issues`
- `agent_tool_traces`

All artifacts should be versioned:

- `run_id`
- `phase`
- `attempt`
- `parent_artifact_id`
- `model`
- `provider`
- `prompt_hash`
- `input_artifact_ids`
- `created_at`

Debuggability must be first-class. If a draft is bad, we should be able to inspect:

- what the agent searched
- what it read
- which atoms it created
- why it chose the treatment
- what the writer saw
- what the critic flagged
- what the rewriter changed

## Quality Gates

V2 should use gates at natural boundaries, not after every tiny move.

### Research Gate

Pass if:

- central thesis is source-backed
- high-impact atoms have anchors
- risks are explicit
- important gaps are named

Fail if:

- memo depends on unsupported synthesis
- source coverage is too weak for requested duration
- material cannot support the episode promise

### Treatment Gate

Pass if:

- acts create a real listener journey
- act jobs differ
- omissions are explicit
- long-form duration is justified by material

Fail if:

- act map is just topic buckets
- every act repeats the same explanatory shape
- treatment promises material not in research memo

### Draft Gate

Pass if:

- act/episode is interesting to listen to
- repetition ledger is respected
- explanation is comprehensible
- host dynamics are varied

Fail if:

- text is correct but boring
- text repeats the same rhetorical pattern
- writer reopens already-settled questions
- listener would lose causal continuity

### Fact Gate

Pass if:

- no blocking unsupported factual assertions
- risky interpretation is hedged/attributed
- citation links have locators

Fail if:

- draft overclaims
- citation links do not support wording
- repair introduces new unsupported facts

## Evaluation Plan

V2 should be evaluated as a separate runner, not mixed into V1.

### Test Corpora

Use at least:

1. history/institutional topic, such as Hong Kong banknotes
2. contemporary web-heavy topic
3. debate/compare topic with conflicting views
4. technical/scientific explainer
5. source-poor narrow topic with a long duration request

### Compare Against V1

Generate:

- V1 treatment/plan/script where possible
- V2 memo/treatment/act draft

Evaluate:

- does the episode have a real central question?
- does each act move the listener?
- are repetitions materially reduced?
- is the draft more listenable?
- are facts still recoverably grounded?
- how much cost/time did it take?
- where did the model waste tool calls?

### Human Review Rubric

Score 1-5:

- editorial spine
- source-backedness
- act progression
- listener comprehension
- repetition/rhetorical variety
- host conversation quality
- factual caution
- ending/payoff

### Automated/LLM-Assisted Review

Use evaluator agents for:

- repeated rhetorical move detection
- open loop tracking
- term introduction quality
- unsupported claim sampling
- act job coherence

But do not let automated scores replace listening/review for the first benchmark set.

## Implementation Roadmap

### Milestone 1. V2 Research Memo Only

Build:

- corpus workspace
- corpus MCP/local tools
- source card agent
- editorial research agent
- research auditor

Deliverable:

- V2 research memo and editorial atoms for existing runs.

Success:

- memo is more useful than current research tree/claims for deciding "what episode is this?"

### Milestone 2. Treatment Only

Build:

- story editor agent
- treatment artifact
- treatment audit

Deliverable:

- V2 treatment for a known project.

Success:

- treatment reads like a strong producer plan, not a claim allocation.

### Milestone 3. One-Act Draft

Build:

- act dossier compiler
- act writer
- ledger updater
- act critic/rewrite

Deliverable:

- Act 1 and one middle act drafted from V2 treatment.

Success:

- act is more listenable and less repetitive than V1 without losing factual caution.

### Milestone 4. Full Episode Draft

Build:

- full act loop
- whole-episode critic
- transition rewrites

Deliverable:

- full 30-60 minute text draft.

Success:

- long-form structure survives across acts.

### Milestone 5. Citation and Fact Pass

Build:

- draft claim extractor
- citation linker
- fact verifier
- fact repair

Deliverable:

- source-linked final draft.

Success:

- factual audit is acceptable without degrading the draft into stiff claim prose.

### Milestone 6. Production Runner

Build:

- API/worker integration
- UI debug views for V2 artifacts
- cost controls
- run comparison mode

Deliverable:

- `generation_mode = v1|v2|compare`

## Key Risks

### Risk: Agent Becomes a Black Box

Mitigation:

- store every tool call
- store every artifact version
- require phase summaries
- show trace in debug UI

### Risk: Editorial Synthesis Hallucinates

Mitigation:

- atom confidence labels
- source anchors
- research auditor
- post-draft claim extraction and fact repair

### Risk: Hosted File Search Loses Exact Provenance

Mitigation:

- use hosted file search for discovery
- use local corpus tools for exact locator reads
- final citations must resolve to local source IDs/locators

### Risk: Cost Explosion

Mitigation:

- per-phase budgets
- cached source cards
- cached atoms
- search/read quotas
- budget tool visible to agent
- stop early when corpus cannot support duration

### Risk: Writer Starts Researching Instead of Writing

Mitigation:

- writer receives act dossier
- broad search tools disabled by default in writing
- writer can request clarification but not roam

### Risk: Long Context Makes Model Worse

Mitigation:

- curated dossiers
- ledger memory
- raw read on demand
- no full-corpus prompt dumping

## What V2 Explicitly Rejects

V2 is not:

- more validators around the existing act planner
- a larger claim builder prompt
- "give the writer all claims and hope"
- "let the model access raw Postgres"
- one giant prompt for a 90-minute episode
- replacing evidence with vibes

V2 is:

- agentic research with source-backed editorial artifacts
- treatment-first story construction
- act-level drafting
- whole-episode critique
- post-draft fact extraction and citation repair
- deterministic provenance at the boundaries

## Minimal Prototype Definition

The smallest meaningful prototype is not a full audio pipeline.

It is:

1. select an existing project corpus
2. build source cards
3. run editorial research agent
4. produce research memo
5. produce episode treatment
6. write one full act
7. run critic/rewrite once
8. manually compare against V1 plan/draft

If this prototype does not produce a visibly better act, V2 is wrong or under-specified.

If it does, the current V1 pipeline should be repositioned as a source/evidence backend, not the primary creative architecture.

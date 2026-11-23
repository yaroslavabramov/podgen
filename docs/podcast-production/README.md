# Podcast production architecture

Podgen turns a topic into a researched, scripted and narrated podcast. The episode coordinator runs hosted research, acquires the cited sources, commissions a plan and its review, and sends the accepted plan to the writer. An editorial reader and a fact-checker assess the resulting script independently. An accepted reading outcome can proceed to synthesis and assembly. The exported master still requires listening.

## Read the implementation

| Question | Guide |
| --- | --- |
| How do I prepare, start, inspect or resume an episode? | [Episode CLI](episode-production.md) |
| What source text actually reached a model? | [Acquisition](source-acquisition-runtime.md), [corpus](research-corpus-runtime.md), [source search](source-search.md), [role tools](research-materials-runtime.md) |
| What do the planner, writer and reviewers see? | [Role inputs](08-editorial-inputs.md), [writing profiles](writing-behavior-profiles.md), [planning](planning-v9.md) |
| How does a revision become the final script? | [Editorial recheck](revision-recheck.md), [reading audit](reading-audit-contract.md), [handoff to audio](validated-outcome-and-audio-descriptors.md) |
| How are calls, costs and uncertain outcomes recorded? | [Execution journal](execution-journal.md), [private journal](private-execution-journal-v3.md), [budget recovery](input-budget-recovery.md) |
| What is persisted once, and what is checked again? | [Whole-operation persistence](full-operation-persistence.md), [document publication](document-publication.md), [blob publication](blob-publication.md), [index reuse](reusable-source-index.md) |
| How are saved operations recovered? | [Technical recovery](technical-recovery.md), [planning continuation](planning-continuation.md), [editorial continuation](editorial-continuation.md) |

## Read the design

The numbered documents describe the target product and its acceptance criteria. They include capabilities beyond the current CLI, such as user-facing release approval, independently qualified audio packages and experimentally validated long-form writing strategies.

1. [System contract](00-system-contract.md) defines ownership, identity, workflow and publication.
2. [Sources and evidence](01-sources-and-evidence.md) distinguishes a saved document from a judgment about what it establishes.
3. [Research and editorial direction](02-research-and-editor.md) specifies the working question and its handoff to the writer.
4. [Story and writing](03-story-and-writing.md) covers structure, continuity and revision. [Editorial practice](03-editorial-practice.md) shows how this changes the words a listener hears.
5. [Verification and evaluation](04-verification-and-evaluation.md) covers claims, independent readings and comparative evaluation.
6. [Audio production](05-audio-production.md) defines performance, rendering, editing and listening.
7. [Platform and runtime](06-platform-and-runtime.md) covers execution, persistence and recovery.
8. [Implementation plan](07-implementation-plan.md) orders the work and defines the experiments and end-to-end acceptance cases.

The [design review](REVIEW.md) records decisions shared by several modules. The [evaluation record](../../evals/reports/README.md) explains what has been measured. A successful provider call, a reviewer’s approval and publication of a finished episode are three distinct events.

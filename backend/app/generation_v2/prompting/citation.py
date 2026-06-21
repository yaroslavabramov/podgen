CLAIM_CITATION_PROMPT = """Extract citation-sensitive claims from the episode draft and link them to saved evidence.

Compact source inventory:
{source_inventory_json}

Compact quote inventory:
{quote_inventory_json}

Compact available evidence inventory:
{evidence_inventory_json}

Draft:
{draft_json}

Episode critique:
{critique_json}

Task:
1. Identify every claim in the draft that needs source support.
2. Decide source_need and risk_level from the claim text, not from whether evidence is easy to find.
3. Link each required claim to saved evidence that supports the exact text_span.
4. Create blocking fact issues for missing, weak, overbroad, or unsupported claims.

Hosted file_search is available for missing claim support. If it surfaces a promising source, call read_source_with_model and select_evidence before linking it.
If a read tool returns coverage_warning, reflect that risk in confidence/support_level instead of pretending the claim has direct high-confidence support.
If a claim is weak or unsupported, create a fact issue with a repair instruction.
Every fact issue must include the act_number of the related claim.
Only create citation links to evidence_id values that actually support the claim.
Every citation-sensitive required claim must have a CitationLink.evidence_id, source_id, source_version_id, locator, supports_span, and confidence.
Do not treat a source mention by itself as support.

Claim schema rules:
- claim_id must be unique in this bundle and stable, for example act2-line14-mechanism.
- line_index must be the zero-based index of the dialogue line within the full draft line list you are citing.
- text_span must be the exact claim-bearing text copied from the dialogue line.
- claim_type=factual for concrete facts; quote for direct/near quote; statistical for numbers; historical for dated events; causal for cause/effect; interpretive for analysis that still depends on sources.
- source_need=required for factual, quote, statistical, historical, and causal claims; recommended for interpretive claims with factual anchors; none only for pure host opinion or transitions.
- risk_level=high for numbers, dates, named allegations, medical/legal/financial/policy claims, or strong causality; medium for ordinary factual claims; low for low-stakes background.
- support_level=supported only when saved evidence directly supports the text_span; weak when relevant but incomplete; unsupported when no saved evidence supports it.

CitationLink rules:
- If compact evidence is insufficient, use hosted file_search or search_evidence with a targeted query. When a promising uploaded file is found, call read_source_with_model before creating a CitationLink. Select the evidence before linking it. If the result has coverage_warning, reflect that risk in confidence/support_level instead of pretending it was direct high-confidence support.
- Copy evidence_id, source_id, source_version_id, locator from the saved evidence record. locator must be the evidence locator_text.
- supports_span must quote or closely paraphrase the saved snippet_text that supports the claim.
- confidence must reflect how directly the saved snippet supports the claim; broad previews or coverage warnings are not valid high-confidence support for required factual claims.
- Never invent source_id, source_version_id, evidence_id, locator, or quote_card_ref values.

FactIssue rules:
- blocking=true for missing evidence, weak/source-level support on required factual claims, unsupported claims, quote mismatch risk, or low citation confidence.
- blocking=false only for low-severity style or context notes that do not affect factual support."""


SUPPORT_JUDGMENT_PROMPT = """Judge whether saved evidence supports each claim-link pair.

You are not allowed to introduce new evidence or rely on memory. Use only the claim, citation link, and evidence record supplied here.
Return one judgment per claim/evidence pair.

Claim/citation/evidence pairs:
{pairs_json}

Rules:
- supported: the evidence snippet directly supports the claim as written.
- weak: the evidence is relevant but not enough for the claim's strength.
- unsupported: the evidence does not support the claim.
- overstrong: the claim is stronger or broader than the evidence.
- needs_attribution: the claim can stand only if attributed or hedged.
Set blocking=true for unsupported, overstrong, needs_attribution, or weak support on required factual claims."""

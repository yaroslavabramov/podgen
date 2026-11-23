# Sources and evidence

The source subsystem answers three questions: which document was obtained, what representation the system read, and how a reviewer can reopen the relevant passage. It supplies the researcher, writer and verifier with the same underlying evidence. The meaning attributed to that evidence belongs to the research note or factual review.

## Identity and representation

A source identifies a work. A revision identifies the bytes received during one acquisition. A redirect, a later edition and a separate republication can point to different material even when the titles match.

```text
SourceRevision.payload = {
  source_id, original_blob: BlobRef,
  requested_uri, resolved_uri, redirect_chain,
  acquired_at, response_metadata: {status, content_type, etag?, last_modified?},
  title?, creators: [], publisher?, language?,
  published_at?: {value, precision, basis}, modified_at?: {value, precision, basis},
  applicability?: {valid_from?, valid_until?, geographic_scope?},
  acquisition_kind: http | browser_snapshot | upload | connector,
  acquisition_record_ref,
  rights: {access_class, storage_policy_ref, publication_policy_ref},
  source_metadata_evidence: [], limitations: []
}
```

An extraction has its own version, producer and coverage. Parsing a revised PDF, repeating OCR or choosing a different language track creates another representation of the same original.

```text
SourceRepresentation.payload = {
  source_revision_ref,
  representation_kind: html_text | document_layout | table | dataset | transcript | translation,
  content_blob: BlobRef, canonical_text_blob?: BlobRef,
  extractor: {profile_ref: ArtifactRef, name, version, configuration_hash, model_profile_ref?},
  normalization_version,
  blocks: [{block_id, kind, parent_id?, text_range?, original_anchor, warnings: []}],
  extraction_report: {coverage, missing_regions: [], reading_order_issues: [], warnings: []},
  derived_from_representation_ref?: ArtifactRef,
  language, is_transformation: boolean
}
```

An evidence span binds text to a representation. Text offsets use half-open Unicode code-point ranges. Page selectors need a page origin and render coordinates. Audio selectors need an explicit time base. Dataset cells need the row, column and schema as well as the value. Preserve the exact text and original language for quotations. A translation or paraphrase is a derived assertion.

```text
EvidenceSpan.payload = {
  source_revision_ref, representation_ref,
  selector: TextSelector | TableSelector | DatasetSelector | PageRegionSelector | TimeSelector,
  exact_text?: string, exact_text_hash?: string,
  media_anchor?: {blob_ref, selector},
  context_selector,
  extraction_warnings: [],
  transformation: none | ocr | asr | translation
}
TextSelector = {kind: text, block_ids: [], char_start, char_end, exact, prefix, suffix}
TableSelector = {kind: table, table_block_id, cells: [{row_id, column_id}], header_block_ids: [], footnote_block_ids: []}
DatasetSelector = {kind: dataset, record_ids: [], field_ids: [], original_record_locators: []}
PageRegionSelector = {kind: page_region, page_index, bbox, coordinate_space, render_blob_ref, render_profile_ref}
TimeSelector = {kind: time, track_id, time_base: {numerator, denominator}, origin,
                start_tick, end_tick, stream_offset_ticks, alignment_kind, transcript_block_ids: []}
```

Two URLs can repeat the same account. Record relationships between works and the evidence for them so repeated copies do not count as independent corroboration.

```text
SourceRelation.payload = {
  left_source_ref: ArtifactRef, right_source_ref: ArtifactRef,
  relation: identical_content | mirrors | translates | republishes | cites | same_underlying_dataset,
  basis_refs: [], method: exact_hash | metadata | semantic_review,
  rationale?, reviewer_ref?
}
SourceFamilyAssessment.payload = {
  member_refs: [], common_origin_refs: [], relationship_refs: [],
  scope_description, unknown_relationships: []
}
```

## Acquisition and search

1. Apply the task's source scope and project authorization before sending a request. Fetch bounded content, validate redirects and keep the returned bytes and relevant transport metadata.
2. Extract a readable representation with a pinned profile. A login page, publisher shell or maintenance notice can have plenty of text while providing no article. Record the reason and the available fallback.
3. For a scan, retain the relation to the page image and mark OCR uncertainty. For HTML, retain meaningful structure such as a table’s caption, column labels and units. An excerpt stripped of a denominator can reverse a conclusion.
4. Index the representation with the versioned extraction and retrieval settings. Search results are navigation. Reading an exact passage creates the evidence receipt that can accompany a judgment.
5. If the source changes, keep the old revision and reopen affected conclusions against the new one. Respect access withdrawal in search, previews, model payloads and exports.

The implementation separates [document acquisition](source-acquisition-runtime.md), [source search](source-search.md) and the [model-facing reading tools](research-materials-runtime.md). In a hosted research report, a provider citation establishes that a link was reported. Direct passages come from what Podgen could actually retain and read.

```text
SearchRecord = {record_id, project_id, task_run_id, query, scope, filters,
                provider_profile_ref, searched_at, response_blob_ref, response_hash,
                hits: DiscoveryHit[], tool_execution_id, policy_ref}
DiscoveryHit = {hit_id, ordinal, locator, title?, snippet?, observed_metadata,
                candidate_artifact_ref?: ArtifactRef, rank_components, limitations: []}
```

## Verification cases

Check a permanent URL whose contents change, two editions with the same title, a blocked article with a readable abstract, a copied quotation missing context, and a table whose unit appears outside the selected cells. For a failed document, retain its provenance and explanation. For a real quotation, a reviewer must be able to reconstruct its exact representation and surroundings without relying on the search snippet.

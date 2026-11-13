-- This lock covers trigger installation and backfill. Old INSERT writers wait,
-- then populate the same projection automatically after this migration commits.
LOCK TABLE runtime_records IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE runtime_artifact_refs (
 project_id uuid NOT NULL, record_id uuid NOT NULL,
 artifact_id uuid NOT NULL, revision_id uuid NOT NULL, kind text NOT NULL,
 PRIMARY KEY(project_id,record_id,revision_id,artifact_id,kind),
 FOREIGN KEY(project_id,record_id) REFERENCES runtime_records(project_id,record_id)
);
CREATE INDEX runtime_artifact_consumers ON runtime_artifact_refs(project_id,revision_id,record_id);

CREATE TABLE runtime_runtime_refs (
 project_id uuid NOT NULL, record_id uuid NOT NULL,
 target_record_id uuid NOT NULL, record_kind text NOT NULL,
 PRIMARY KEY(project_id,record_id,target_record_id,record_kind),
 FOREIGN KEY(project_id,record_id) REFERENCES runtime_records(project_id,record_id)
);
CREATE INDEX runtime_runtime_consumers ON runtime_runtime_refs(project_id,target_record_id,record_id);

CREATE TABLE runtime_blob_refs (
 project_id uuid NOT NULL, record_id uuid NOT NULL,
 storage_key text NOT NULL, sha256 text NOT NULL, byte_length bigint NOT NULL, media_type text NOT NULL,
 PRIMARY KEY(project_id,record_id,storage_key,sha256,byte_length,media_type),
 FOREIGN KEY(project_id,record_id) REFERENCES runtime_records(project_id,record_id)
);
CREATE INDEX runtime_blob_consumers ON runtime_blob_refs(project_id,sha256,record_id);

-- Reverse access paths for the existing canonical projections; do not duplicate them.
CREATE INDEX revision_privacy_consumers ON revision_links(project_id,input_id,consumer_id);
CREATE INDEX revalidation_privacy_consumers ON revalidation_privacy_links(project_id,input_id,subject_id);
CREATE INDEX artifact_runtime_consumers ON runtime_links(project_id,record_id,revision_id);
CREATE INDEX artifact_blob_aliases ON artifact_blobs(project_id,storage_key,revision_id);
CREATE INDEX catalog_asset_hash_lookup ON catalog_publications USING gin ((config->'published_assets') jsonb_path_ops);

CREATE FUNCTION podgen_runtime_reference_kind(value json) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
 SELECT CASE WHEN json_typeof(value) = 'object' THEN CASE
   WHEN ARRAY(SELECT key FROM json_object_keys(value) AS key ORDER BY key) = ARRAY['artifact_id','kind','revision_id'] THEN 'artifact'
   WHEN ARRAY(SELECT key FROM json_object_keys(value) AS key ORDER BY key) = ARRAY['record_id','record_kind'] THEN 'runtime'
   WHEN ARRAY(SELECT key FROM json_object_keys(value) AS key ORDER BY key) = ARRAY['byte_length','media_type','sha256','storage_key'] THEN 'blob'
 END END
$$;

-- PostgreSQL's json iterators decode every scalar, including unrelated text.
-- NUL cannot occur in SQL-backed reference metadata, but is legitimate raw text.
-- Replace only complete string tokens containing an active NUL escape in this
-- temporary traversal view. Escaped backslash pairs stay literal. Unknown keys
-- remain unknown; NUL in an exact reference value becomes NULL and fails its
-- NOT NULL constraint. The original runtime payload is never rewritten.
CREATE FUNCTION podgen_runtime_reference_document(document json) RETURNS json
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $function$
 SELECT regexp_replace(
   regexp_replace(document::text,
     $pattern$"(?:[^"\\]|\\.)*\\u0000(?:[^"\\]|\\.)*"([[:space:]]*:)$pattern$,
     '"__podgen_non_reference_nul_key__"\1', 'g'),
   $pattern$"(?:[^"\\]|\\.)*\\u0000(?:[^"\\]|\\.)*"$pattern$, 'null', 'g')::json
$function$;

-- Match walk_values' exact shapes, including its stop at a recognized typed ref.
-- Diagnostic dictionaries, serialized JSON strings and raw UUIDs are not references.
CREATE FUNCTION podgen_runtime_references(document json)
RETURNS TABLE(reference_kind text, reference json)
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
 WITH RECURSIVE tree(value) AS (
   SELECT podgen_runtime_reference_document(document)
   UNION ALL
   SELECT child.value FROM tree AS parent CROSS JOIN LATERAL (
     SELECT item.value FROM json_each(CASE
       WHEN json_typeof(parent.value) = 'object'
        AND podgen_runtime_reference_kind(parent.value) IS NULL THEN parent.value
       ELSE '{}'::json END) AS item
     UNION ALL
     SELECT item.value FROM json_array_elements(CASE
       WHEN json_typeof(parent.value) = 'array' THEN parent.value
       ELSE '[]'::json END) AS item
   ) AS child
 )
 SELECT podgen_runtime_reference_kind(value), value
 FROM tree WHERE podgen_runtime_reference_kind(value) IS NOT NULL
$$;

-- walk_values validates/coerces refs but persists the caller's original JSON.
-- Normalize the accepted scalar representations only in the typed projection.
CREATE FUNCTION podgen_runtime_reference_uuid(value text) RETURNS uuid
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
 SELECT (CASE WHEN value ~ '^urn:uuid:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
   THEN substring(value FROM 10) ELSE value END)::uuid
$$;

CREATE FUNCTION podgen_runtime_reference_length(value json) RETURNS bigint
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $function$
DECLARE lexeme text; floating double precision; result bigint;
BEGIN
 CASE json_typeof(value)
 WHEN 'boolean' THEN RETURN CASE value::text WHEN 'true' THEN 1 ELSE 0 END;
 WHEN 'number' THEN
   lexeme := value::text;
   IF lexeme ~ '[.eE]' THEN
     -- Match int(float), not decimal rounding of its serialized spelling.
     floating := lexeme::double precision;
     IF floating <> trunc(floating) OR floating < 0 THEN
       RAISE EXCEPTION 'invalid runtime reference byte_length' USING ERRCODE='22023';
     END IF;
     RETURN floating::bigint;
   END IF;
 WHEN 'string' THEN
   -- Rust/Pydantic's Unicode White_Space trim; the raw string stays in payload.
   lexeme := btrim(value #>> '{}',
     U&'\0009\000A\000B\000C\000D\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000');
   IF lexeme !~ '^[+-]?[0-9]+(_[0-9]+)*(\.0+)?$' THEN
     RAISE EXCEPTION 'invalid runtime reference byte_length' USING ERRCODE='22023';
   END IF;
   lexeme := replace(split_part(lexeme,'.',1),'_','');
 ELSE RETURN NULL;
 END CASE;
 result := lexeme::bigint;
 IF result < 0 THEN
   RAISE EXCEPTION 'invalid runtime reference byte_length' USING ERRCODE='22023';
 END IF;
 RETURN result;
END
$function$;

CREATE FUNCTION project_runtime_references() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 WITH refs AS MATERIALIZED (SELECT * FROM podgen_runtime_references(NEW.payload)),
 artifacts AS (
   INSERT INTO runtime_artifact_refs
   SELECT NEW.project_id,NEW.record_id,podgen_runtime_reference_uuid(reference->>'artifact_id'),
     podgen_runtime_reference_uuid(reference->>'revision_id'),reference->>'kind'
   FROM refs WHERE reference_kind='artifact' ON CONFLICT DO NOTHING
 ), runtimes AS (
   INSERT INTO runtime_runtime_refs
   SELECT NEW.project_id,NEW.record_id,podgen_runtime_reference_uuid(reference->>'record_id'),reference->>'record_kind'
   FROM refs WHERE reference_kind='runtime' ON CONFLICT DO NOTHING
 )
 INSERT INTO runtime_blob_refs
 SELECT NEW.project_id,NEW.record_id,reference->>'storage_key',reference->>'sha256',
   podgen_runtime_reference_length(reference->'byte_length'),reference->>'media_type'
 FROM refs WHERE reference_kind='blob' ON CONFLICT DO NOTHING;
 RETURN NEW;
END
$$;
CREATE TRIGGER runtime_reference_projection AFTER INSERT ON runtime_records
FOR EACH ROW EXECUTE FUNCTION project_runtime_references();

-- One historical payload pass. MATERIALIZED may spill under PostgreSQL work_mem;
-- no project history is loaded into the migration client's Python process.
WITH refs AS MATERIALIZED (
 SELECT owner.project_id,owner.record_id,ref.reference_kind,ref.reference
 FROM runtime_records AS owner CROSS JOIN LATERAL podgen_runtime_references(owner.payload) AS ref
), artifacts AS (
 INSERT INTO runtime_artifact_refs
 SELECT project_id,record_id,podgen_runtime_reference_uuid(reference->>'artifact_id'),
   podgen_runtime_reference_uuid(reference->>'revision_id'),reference->>'kind'
 FROM refs WHERE reference_kind='artifact' ON CONFLICT DO NOTHING
), runtimes AS (
 INSERT INTO runtime_runtime_refs
 SELECT project_id,record_id,podgen_runtime_reference_uuid(reference->>'record_id'),reference->>'record_kind'
 FROM refs WHERE reference_kind='runtime' ON CONFLICT DO NOTHING
)
INSERT INTO runtime_blob_refs
SELECT project_id,record_id,reference->>'storage_key',reference->>'sha256',
  podgen_runtime_reference_length(reference->'byte_length'),reference->>'media_type'
FROM refs WHERE reference_kind='blob' ON CONFLICT DO NOTHING;

DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['runtime_artifact_refs','runtime_runtime_refs','runtime_blob_refs'] LOOP
   EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()', t);
 END LOOP;
END $$;

-- Existing witness rows remain intact. New/old writers reserve one membership
-- key in the same statement as its witness, so duplicate events do not grow an
-- event-by-hash matrix. Authorization still reads evaluator_blob_classifications.
LOCK TABLE evaluator_blob_classifications IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE evaluator_protected_hashes (
 project_id uuid NOT NULL REFERENCES projects,
 sha256 text NOT NULL,
 PRIMARY KEY(project_id,sha256)
);
INSERT INTO evaluator_protected_hashes
SELECT DISTINCT project_id,sha256 FROM evaluator_blob_classifications;

CREATE FUNCTION reserve_evaluator_protected_hash() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO evaluator_protected_hashes(project_id,sha256)
 VALUES(NEW.project_id,NEW.sha256) ON CONFLICT DO NOTHING;
 IF NOT FOUND THEN RETURN NULL; END IF;
 RETURN NEW;
END
$$;
CREATE TRIGGER evaluator_hash_membership BEFORE INSERT ON evaluator_blob_classifications
FOR EACH ROW EXECUTE FUNCTION reserve_evaluator_protected_hash();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON evaluator_protected_hashes
FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

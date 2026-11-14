ALTER TABLE runtime_records ADD COLUMN reference_policy text NOT NULL DEFAULT 'legacy/1';
ALTER TABLE runtime_records ADD COLUMN reference_manifest jsonb;
ALTER TABLE runtime_records ADD COLUMN journal_binding jsonb;
ALTER TABLE runtime_records ADD COLUMN journal_fingerprint text;
ALTER TABLE runtime_records ADD CONSTRAINT acquisition_reference_contract CHECK (
 (reference_policy='legacy/1' AND reference_manifest IS NULL AND journal_binding IS NULL AND journal_fingerprint IS NULL)
 OR (reference_policy='acquisition-journal/2' AND record_kind='acquisition_record'
     AND audience='project' AND execution_input_set_id IS NULL
     AND reference_manifest IS NOT NULL AND journal_binding IS NOT NULL
     AND jsonb_typeof(reference_manifest)='array' AND jsonb_typeof(journal_binding)='object'
     AND journal_fingerprint IS NOT NULL));
ALTER TABLE artifact_revisions ADD COLUMN reference_policy text NOT NULL DEFAULT 'legacy/1'
 CHECK(reference_policy IN ('legacy/1','source-document/2'));
ALTER TABLE runtime_artifact_refs ADD COLUMN read_required boolean NOT NULL DEFAULT true;
ALTER TABLE runtime_artifact_refs ADD COLUMN privacy_required boolean NOT NULL DEFAULT true;
ALTER TABLE runtime_runtime_refs ADD COLUMN read_required boolean NOT NULL DEFAULT true;
ALTER TABLE runtime_runtime_refs ADD COLUMN privacy_required boolean NOT NULL DEFAULT true;
ALTER TABLE runtime_links ADD COLUMN read_required boolean NOT NULL DEFAULT true;
ALTER TABLE runtime_links ADD COLUMN privacy_required boolean NOT NULL DEFAULT true;
DROP TRIGGER runtime_reference_projection ON runtime_records;
CREATE TRIGGER runtime_reference_projection AFTER INSERT ON runtime_records
 FOR EACH ROW WHEN (NEW.execution_input_set_id IS NULL AND NEW.reference_policy='legacy/1')
 EXECUTE FUNCTION project_runtime_references();

CREATE FUNCTION project_acquisition_references() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO runtime_artifact_refs(project_id,record_id,artifact_id,revision_id,kind,read_required,privacy_required)
 SELECT NEW.project_id,NEW.record_id,(item->'ref'->>'artifact_id')::uuid,
        (item->'ref'->>'revision_id')::uuid,item->'ref'->>'kind',
        (item->>'read')::boolean,(item->>'privacy')::boolean
 FROM jsonb_array_elements(NEW.reference_manifest) item WHERE item->>'kind'='artifact'
 ON CONFLICT DO NOTHING;
 INSERT INTO runtime_runtime_refs(project_id,record_id,target_record_id,record_kind,read_required,privacy_required)
 SELECT NEW.project_id,NEW.record_id,(item->'ref'->>'record_id')::uuid,item->'ref'->>'record_kind',
        (item->>'read')::boolean,(item->>'privacy')::boolean
 FROM jsonb_array_elements(NEW.reference_manifest) item WHERE item->>'kind'='runtime'
 ON CONFLICT DO NOTHING;
 INSERT INTO runtime_blob_refs(project_id,record_id,storage_key,sha256,byte_length,media_type,is_owned)
 SELECT NEW.project_id,NEW.record_id,item->'ref'->>'storage_key',item->'ref'->>'sha256',
        (item->'ref'->>'byte_length')::bigint,item->'ref'->>'media_type',(item->>'owned')::boolean
 FROM jsonb_array_elements(NEW.reference_manifest) item WHERE item->>'kind'='blob'
 ON CONFLICT DO NOTHING;
 RETURN NEW;
END $$;
CREATE TRIGGER acquisition_reference_projection AFTER INSERT ON runtime_records
 FOR EACH ROW WHEN (NEW.reference_policy='acquisition-journal/2')
 EXECUTE FUNCTION project_acquisition_references();

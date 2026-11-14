ALTER TABLE evaluation_writing_runs ADD COLUMN execution_storage text NOT NULL DEFAULT 'legacy';
ALTER TABLE evaluation_writing_runs ADD CONSTRAINT execution_storage_contract
 CHECK(execution_storage IN ('legacy','private/3'));

CREATE TABLE execution_entries (
 project_id uuid NOT NULL,run_id uuid NOT NULL,entry_id uuid NOT NULL,
 revision bigint NOT NULL CHECK(revision>0),commit_key text NOT NULL,content_hash text NOT NULL,
 checkpoint_kind text NOT NULL,operation_id uuid NOT NULL,attempt_id uuid NOT NULL,step_id uuid,
 step_index integer,observation_no bigint NOT NULL DEFAULT 0 CHECK(observation_no>=0),
 input_set_id uuid NOT NULL,projection_version integer NOT NULL DEFAULT 3 CHECK(projection_version=3),
 data json NOT NULL,archive_payloads json NOT NULL DEFAULT '[]',
 charge_changes jsonb NOT NULL DEFAULT '[]',charge_operations uuid[] NOT NULL DEFAULT '{}',
 archive_views jsonb NOT NULL DEFAULT '[]',archive_ids uuid[] NOT NULL DEFAULT '{}',
 owned_objects jsonb NOT NULL DEFAULT '{}',object_origins jsonb NOT NULL DEFAULT '{}',
 owned_hashes text[] NOT NULL DEFAULT '{}',parent_entries uuid[] NOT NULL DEFAULT '{}',
 PRIMARY KEY(project_id,entry_id),UNIQUE(project_id,run_id,entry_id),
 UNIQUE(project_id,run_id,revision),UNIQUE(project_id,run_id,commit_key),
 FOREIGN KEY(project_id,run_id) REFERENCES evaluation_writing_runs(project_id,run_id),
 FOREIGN KEY(project_id,run_id,input_set_id) REFERENCES execution_input_sets(project_id,run_id,set_id)
);
CREATE INDEX execution_entry_charge ON execution_entries USING gin(charge_operations);
CREATE INDEX execution_entry_inputs ON execution_entries(project_id,input_set_id,entry_id);
CREATE INDEX execution_entry_attempt ON execution_entries(project_id,run_id,attempt_id,revision);
CREATE INDEX execution_entry_step ON execution_entries(project_id,run_id,step_id,revision DESC);
CREATE INDEX execution_entry_turn ON execution_entries(project_id,run_id,attempt_id,step_index,revision)
 WHERE step_index IS NOT NULL;
CREATE INDEX execution_entry_operation ON execution_entries(project_id,run_id,operation_id,checkpoint_kind,observation_no);
CREATE INDEX execution_entry_archive_ids ON execution_entries USING gin(archive_ids);
CREATE INDEX execution_entry_owned_hashes ON execution_entries USING gin(owned_hashes);
CREATE INDEX execution_entry_parents ON execution_entries USING gin(parent_entries);
CREATE INDEX execution_attempt_stage ON execution_entries(project_id,run_id,(data->'attempt'->>'stage'),revision)
 WHERE data->'attempt' IS NOT NULL;
CREATE TRIGGER immutable_execution_entry BEFORE UPDATE OR DELETE ON execution_entries
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE TABLE execution_heads (
 project_id uuid NOT NULL,run_id uuid NOT NULL,revision bigint NOT NULL CHECK(revision>0),
 active_attempt_id uuid NOT NULL,active_step_id uuid,step_index integer,
 phase text NOT NULL,last_entry_id uuid NOT NULL,request_entry_id uuid,provider_entry_id uuid,submit_entry_id uuid,
 pending_parent_entry_id uuid,input_set_id uuid NOT NULL,
 request_hash text,configuration_hash text,count_identity text,count_entry_id uuid,
 provider_id text,observation_no bigint NOT NULL DEFAULT 0,accounting jsonb NOT NULL DEFAULT '{}',
 PRIMARY KEY(project_id,run_id),
 FOREIGN KEY(project_id,run_id) REFERENCES evaluation_writing_runs(project_id,run_id),
 FOREIGN KEY(project_id,run_id,last_entry_id) REFERENCES execution_entries(project_id,run_id,entry_id),
 FOREIGN KEY(project_id,run_id,request_entry_id) REFERENCES execution_entries(project_id,run_id,entry_id),
 FOREIGN KEY(project_id,run_id,provider_entry_id) REFERENCES execution_entries(project_id,run_id,entry_id),
 FOREIGN KEY(project_id,run_id,submit_entry_id) REFERENCES execution_entries(project_id,run_id,entry_id),
 FOREIGN KEY(project_id,run_id,count_entry_id) REFERENCES execution_entries(project_id,run_id,entry_id),
 FOREIGN KEY(project_id,run_id,pending_parent_entry_id) REFERENCES execution_entries(project_id,run_id,entry_id),
 FOREIGN KEY(project_id,run_id,input_set_id) REFERENCES execution_input_sets(project_id,run_id,set_id)
);

CREATE TABLE execution_objects (
 project_id uuid NOT NULL,sha256 text NOT NULL,media_type text NOT NULL,
 byte_length bigint NOT NULL CHECK(byte_length>=0),storage_key text NOT NULL,first_entry_id uuid NOT NULL,
 PRIMARY KEY(project_id,sha256,media_type),UNIQUE(project_id,storage_key),
 FOREIGN KEY(project_id,first_entry_id) REFERENCES execution_entries(project_id,entry_id)
 DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX execution_object_sha ON execution_objects(project_id,sha256);
CREATE TRIGGER immutable_execution_object BEFORE UPDATE OR DELETE ON execution_objects
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE TABLE execution_entry_denials (
 project_id uuid NOT NULL,entry_id uuid NOT NULL,event_id uuid NOT NULL,reason text NOT NULL,
 PRIMARY KEY(project_id,entry_id),
 FOREIGN KEY(project_id,entry_id) REFERENCES execution_entries(project_id,entry_id)
);
CREATE TRIGGER immutable_execution_denial BEFORE UPDATE OR DELETE ON execution_entry_denials
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE TABLE semantic_execution_links (
 project_id uuid NOT NULL,runtime_record_id uuid,artifact_revision_id uuid,entry_id uuid NOT NULL,
 CHECK(num_nonnulls(runtime_record_id,artifact_revision_id)=1),
 FOREIGN KEY(project_id,runtime_record_id) REFERENCES runtime_records(project_id,record_id),
 FOREIGN KEY(project_id,artifact_revision_id) REFERENCES artifact_revisions(project_id,revision_id),
 FOREIGN KEY(project_id,entry_id) REFERENCES execution_entries(project_id,entry_id)
);
CREATE UNIQUE INDEX semantic_execution_runtime ON semantic_execution_links(project_id,runtime_record_id,entry_id)
 WHERE runtime_record_id IS NOT NULL;
CREATE UNIQUE INDEX semantic_execution_artifact ON semantic_execution_links(project_id,artifact_revision_id,entry_id)
 WHERE artifact_revision_id IS NOT NULL;
CREATE INDEX semantic_execution_source ON semantic_execution_links(project_id,entry_id);
CREATE TRIGGER immutable_semantic_execution_link BEFORE UPDATE OR DELETE ON semantic_execution_links
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

ALTER TABLE evaluator_blob_classifications ADD COLUMN execution_entry_id uuid;
ALTER TABLE evaluator_blob_classifications ADD CONSTRAINT evaluator_execution_witness
 FOREIGN KEY(project_id,execution_entry_id) REFERENCES execution_entries(project_id,entry_id);
ALTER TABLE evaluator_blob_classifications DROP CONSTRAINT evaluator_blob_classifications_check;
ALTER TABLE evaluator_blob_classifications ADD CONSTRAINT evaluator_single_witness
 CHECK(num_nonnulls(artifact_revision_id,runtime_record_id,execution_entry_id)=1);
CREATE UNIQUE INDEX evaluator_bytes_execution ON evaluator_blob_classifications(project_id,sha256,execution_entry_id)
 WHERE execution_entry_id IS NOT NULL;
CREATE VIEW protected_byte_hashes AS
 SELECT project_id,sha256 FROM evaluator_protected_hashes
 UNION SELECT project_id,sha256 FROM execution_objects;

CREATE INDEX IF NOT EXISTS evaluation_event_archive_order ON evaluation_invocation_events(project_id,invocation_id,sequence);

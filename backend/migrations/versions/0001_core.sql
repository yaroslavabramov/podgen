CREATE TABLE projects (
 project_id uuid PRIMARY KEY, namespace_kind text NOT NULL CHECK(namespace_kind IN ('tenant','catalog')),
 validity_version bigint NOT NULL DEFAULT 0
);
CREATE TABLE project_members (
 project_id uuid NOT NULL REFERENCES projects, actor_id uuid NOT NULL,
 role text NOT NULL CHECK(role IN ('owner','editor','viewer','evaluator','operator')),
 PRIMARY KEY(project_id,actor_id)
);
CREATE TABLE episodes (
 project_id uuid NOT NULL REFERENCES projects, episode_id uuid NOT NULL,
 PRIMARY KEY(project_id,episode_id)
);
CREATE TABLE artifacts (
 project_id uuid NOT NULL REFERENCES projects, artifact_id uuid NOT NULL, kind text NOT NULL,
 PRIMARY KEY(project_id,artifact_id), UNIQUE(project_id,artifact_id,kind)
);
CREATE TABLE artifact_revisions (
 project_id uuid NOT NULL, revision_id uuid NOT NULL UNIQUE, artifact_id uuid NOT NULL, kind text NOT NULL,
 episode_id uuid, schema_version text NOT NULL, payload jsonb NOT NULL,
 input_refs jsonb NOT NULL, producer jsonb NOT NULL, content_hash text NOT NULL,
 created_at timestamptz NOT NULL, audience text NOT NULL CHECK(audience IN ('project','evaluator')),
 operation_id uuid NOT NULL, operation_fingerprint text NOT NULL,
 PRIMARY KEY(project_id,revision_id), UNIQUE(project_id,revision_id,artifact_id,kind),
 UNIQUE(project_id,operation_id),
 FOREIGN KEY(project_id,artifact_id,kind) REFERENCES artifacts(project_id,artifact_id,kind),
 FOREIGN KEY(project_id,episode_id) REFERENCES episodes(project_id,episode_id)
);
CREATE TABLE revision_links (
 project_id uuid NOT NULL, consumer_id uuid NOT NULL, input_id uuid NOT NULL,
 PRIMARY KEY(project_id,consumer_id,input_id),
 FOREIGN KEY(project_id,consumer_id) REFERENCES artifact_revisions(project_id,revision_id),
 FOREIGN KEY(project_id,input_id) REFERENCES artifact_revisions(project_id,revision_id),
 CHECK(consumer_id <> input_id)
);
CREATE TABLE artifact_dependencies (
 project_id uuid NOT NULL, consumer_id uuid NOT NULL, input_id uuid NOT NULL,
 relation text NOT NULL CHECK(relation IN ('uses_content','uses_order','applies_policy','renders','verifies','lineage')),
 selector jsonb NOT NULL,
 PRIMARY KEY(project_id,consumer_id,input_id,relation),
 FOREIGN KEY(project_id,consumer_id) REFERENCES artifact_revisions(project_id,revision_id),
 FOREIGN KEY(project_id,input_id) REFERENCES artifact_revisions(project_id,revision_id)
);
CREATE TABLE blobs (
 project_id uuid NOT NULL REFERENCES projects, storage_key text NOT NULL, sha256 text NOT NULL,
 byte_length bigint NOT NULL CHECK(byte_length >= 0), media_type text NOT NULL,
 PRIMARY KEY(project_id,storage_key)
);
CREATE TABLE artifact_blobs (
 project_id uuid NOT NULL, revision_id uuid NOT NULL, storage_key text NOT NULL, purpose text NOT NULL,
 PRIMARY KEY(project_id,revision_id,purpose),
 FOREIGN KEY(project_id,revision_id) REFERENCES artifact_revisions(project_id,revision_id),
 FOREIGN KEY(project_id,storage_key) REFERENCES blobs(project_id,storage_key)
);
CREATE TABLE runtime_records (
 project_id uuid NOT NULL REFERENCES projects, record_id uuid NOT NULL,
 record_kind text NOT NULL, payload jsonb NOT NULL, audience text NOT NULL,
 PRIMARY KEY(project_id,record_id), UNIQUE(project_id,record_id,record_kind)
);
CREATE TABLE runtime_links (
 project_id uuid NOT NULL, revision_id uuid NOT NULL, record_id uuid NOT NULL, record_kind text NOT NULL,
 PRIMARY KEY(project_id,revision_id,record_id),
 FOREIGN KEY(project_id,revision_id) REFERENCES artifact_revisions(project_id,revision_id),
 FOREIGN KEY(project_id,record_id,record_kind) REFERENCES runtime_records(project_id,record_id,record_kind)
);
CREATE TABLE catalog_publications (
 publication_id uuid PRIMARY KEY, project_id uuid NOT NULL, revision_id uuid NOT NULL,
 config jsonb NOT NULL, publisher_id uuid NOT NULL, created_at timestamptz NOT NULL,
 UNIQUE(project_id,revision_id),
 FOREIGN KEY(project_id,revision_id) REFERENCES artifact_revisions(project_id,revision_id)
);
CREATE TABLE catalog_revocations (
 publication_id uuid PRIMARY KEY REFERENCES catalog_publications, reason text NOT NULL, created_at timestamptz NOT NULL
);
CREATE TABLE catalog_references (
 project_id uuid NOT NULL, consumer_id uuid NOT NULL, publication_id uuid NOT NULL REFERENCES catalog_publications,
 PRIMARY KEY(project_id,consumer_id,publication_id),
 FOREIGN KEY(project_id,consumer_id) REFERENCES artifact_revisions(project_id,revision_id)
);
CREATE TABLE acceptance_records (
 project_id uuid NOT NULL, record_id uuid NOT NULL, subject_id uuid NOT NULL,
 check_kind text NOT NULL, decision text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(project_id,record_id),
 FOREIGN KEY(project_id,subject_id) REFERENCES artifact_revisions(project_id,revision_id)
);
CREATE TABLE revocations (
 project_id uuid NOT NULL, revocation_id uuid NOT NULL, acceptance_record_id uuid NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(project_id,revocation_id),
 FOREIGN KEY(project_id,acceptance_record_id) REFERENCES acceptance_records(project_id,record_id)
);
CREATE TABLE invalidations (
 project_id uuid NOT NULL, event_id uuid NOT NULL, revision_id uuid NOT NULL,
 reason text NOT NULL, category text NOT NULL CHECK(category IN ('quality','access')),
 PRIMARY KEY(project_id,event_id),
 FOREIGN KEY(project_id,revision_id) REFERENCES artifact_revisions(project_id,revision_id)
);
CREATE TABLE integration_events (
 project_id uuid NOT NULL, event_id uuid NOT NULL, decision_id uuid NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(project_id,event_id),
 FOREIGN KEY(project_id,decision_id) REFERENCES artifact_revisions(project_id,revision_id)
);
CREATE TABLE dependency_revalidations (
 project_id uuid NOT NULL, event_id uuid NOT NULL, subject_id uuid NOT NULL,
 assessment_id uuid NOT NULL, acceptance_record_id uuid, integration_event_id uuid, payload jsonb NOT NULL,
 CHECK((acceptance_record_id IS NULL) <> (integration_event_id IS NULL)),
 PRIMARY KEY(project_id,event_id),
 FOREIGN KEY(project_id,subject_id) REFERENCES artifact_revisions(project_id,revision_id),
 FOREIGN KEY(project_id,assessment_id) REFERENCES artifact_revisions(project_id,revision_id),
 FOREIGN KEY(project_id,acceptance_record_id) REFERENCES acceptance_records(project_id,record_id),
 FOREIGN KEY(project_id,integration_event_id) REFERENCES integration_events(project_id,event_id)
);
CREATE TABLE branch_heads (
 project_id uuid NOT NULL, episode_id uuid NOT NULL, branch_id uuid NOT NULL,
 editorial_state_id uuid NOT NULL, lock_version bigint NOT NULL, generation_epoch bigint NOT NULL,
 PRIMARY KEY(project_id,episode_id,branch_id),
 FOREIGN KEY(project_id,episode_id) REFERENCES episodes(project_id,episode_id),
 FOREIGN KEY(project_id,editorial_state_id) REFERENCES artifact_revisions(project_id,revision_id)
);
CREATE TABLE operational_records (
 project_id uuid NOT NULL REFERENCES projects, record_kind text NOT NULL, record_id uuid NOT NULL,
 parent_record_id uuid, payload jsonb NOT NULL, PRIMARY KEY(project_id,record_kind,record_id)
);
CREATE TABLE outbox (
 event_id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects, event_kind text NOT NULL,
 payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX dependency_inputs ON artifact_dependencies(project_id,input_id);
CREATE INDEX artifact_kind ON artifact_revisions(project_id,kind);
CREATE INDEX invalidation_target ON invalidations(project_id,revision_id);
CREATE OR REPLACE FUNCTION reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable_history: %', TG_TABLE_NAME; END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['artifacts','artifact_revisions','revision_links','artifact_dependencies',
 'blobs','artifact_blobs','runtime_records','runtime_links','catalog_publications','catalog_revocations',
 'catalog_references','acceptance_records','revocations','invalidations','dependency_revalidations',
 'operational_records','integration_events','outbox'] LOOP
 EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_history_mutation()',t);
 END LOOP;
END $$;

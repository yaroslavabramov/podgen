CREATE TABLE execution_input_sets (
 project_id uuid NOT NULL, run_id uuid NOT NULL, set_id uuid NOT NULL,
 generation bigint NOT NULL CHECK(generation>=0), identity text NOT NULL,
 roots json NOT NULL,
 PRIMARY KEY(project_id,run_id,set_id), UNIQUE(project_id,set_id),
 UNIQUE(project_id,run_id,generation), UNIQUE(project_id,run_id,identity),
 FOREIGN KEY(project_id,run_id) REFERENCES evaluation_writing_runs(project_id,run_id)
);
CREATE TRIGGER immutable_execution_inputs BEFORE UPDATE OR DELETE ON execution_input_sets
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TABLE execution_input_expansions (
 project_id uuid NOT NULL, run_id uuid NOT NULL, set_id uuid NOT NULL,
 owner_versions jsonb NOT NULL, resource_hash text NOT NULL,
 PRIMARY KEY(project_id,run_id,set_id),
 FOREIGN KEY(project_id,run_id,set_id) REFERENCES execution_input_sets(project_id,run_id,set_id)
);
CREATE TABLE execution_input_resources (
 project_id uuid NOT NULL, run_id uuid NOT NULL, set_id uuid NOT NULL,
 purpose text NOT NULL CHECK(purpose IN ('evaluation','generation')),
 resource_kind text NOT NULL CHECK(resource_kind IN ('artifact','runtime','blob','binding','catalog','membership')),
 owner_project_id uuid NOT NULL, identity text NOT NULL, binding jsonb NOT NULL,
 read_required boolean NOT NULL, privacy_required boolean NOT NULL,
 PRIMARY KEY(project_id,run_id,set_id,purpose,resource_kind,owner_project_id,identity),
 FOREIGN KEY(project_id,run_id,set_id) REFERENCES execution_input_sets(project_id,run_id,set_id)
);
CREATE INDEX execution_resource_consumers ON execution_input_resources(owner_project_id,resource_kind,identity,project_id,set_id);
CREATE INDEX execution_resource_hash_consumers ON execution_input_resources(owner_project_id,(binding->>'sha256'),project_id,set_id)
 WHERE resource_kind='blob';
ALTER TABLE runtime_records ADD COLUMN execution_run_id uuid;
ALTER TABLE runtime_records ADD COLUMN execution_input_set_id uuid;
ALTER TABLE runtime_records ADD CONSTRAINT complete_execution_binding CHECK (
 (execution_run_id IS NULL) = (execution_input_set_id IS NULL));
ALTER TABLE runtime_records ADD CONSTRAINT execution_record_owner
 FOREIGN KEY(project_id,execution_run_id,execution_input_set_id)
 REFERENCES execution_input_sets(project_id,run_id,set_id);
CREATE INDEX execution_set_records ON runtime_records(project_id,execution_input_set_id,record_id)
 WHERE execution_input_set_id IS NOT NULL;
DROP TRIGGER runtime_reference_projection ON runtime_records;
CREATE TRIGGER runtime_reference_projection AFTER INSERT ON runtime_records
 FOR EACH ROW WHEN (NEW.execution_input_set_id IS NULL)
 EXECUTE FUNCTION project_runtime_references();

ALTER TABLE runtime_records ADD CONSTRAINT execution_evaluator_audience
 CHECK(execution_input_set_id IS NULL OR audience='evaluator');
ALTER TABLE runtime_blob_refs ADD COLUMN is_owned boolean NOT NULL DEFAULT true;
CREATE FUNCTION check_execution_input_extension() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE previous execution_input_sets%ROWTYPE;
BEGIN
 SELECT * INTO previous FROM execution_input_sets
 WHERE project_id=NEW.project_id AND run_id=NEW.run_id
 ORDER BY generation DESC LIMIT 1;
 IF FOUND THEN
   IF NEW.generation<>previous.generation+1 OR NOT(NEW.roots::jsonb @> previous.roots::jsonb) THEN
     RAISE EXCEPTION 'execution inputs must extend their exact predecessor';
   END IF;
 ELSIF NEW.generation<>0 THEN
   RAISE EXCEPTION 'execution inputs must start at generation zero';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER execution_input_extension BEFORE INSERT ON execution_input_sets
 FOR EACH ROW EXECUTE FUNCTION check_execution_input_extension();

CREATE TABLE execution_record_withdrawals (
 project_id uuid NOT NULL,record_id uuid NOT NULL,event_id uuid NOT NULL,reason text NOT NULL,
 PRIMARY KEY(project_id,record_id),
 FOREIGN KEY(project_id,record_id) REFERENCES runtime_records(project_id,record_id)
);
CREATE TRIGGER immutable_execution_withdrawals BEFORE UPDATE OR DELETE ON execution_record_withdrawals
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TABLE execution_provider_calls (
 project_id uuid NOT NULL,run_id uuid NOT NULL,response_record_id uuid NOT NULL,
 physical_invocation_id uuid NOT NULL,request_record_id uuid NOT NULL,
 call_id text NOT NULL,arguments_hash text NOT NULL,tool_name text NOT NULL,
 PRIMARY KEY(project_id,response_record_id,call_id),
 FOREIGN KEY(project_id,run_id) REFERENCES evaluation_writing_runs(project_id,run_id),
 FOREIGN KEY(project_id,response_record_id) REFERENCES runtime_records(project_id,record_id),
 FOREIGN KEY(project_id,request_record_id) REFERENCES runtime_records(project_id,record_id)
);
CREATE INDEX execution_physical_calls ON execution_provider_calls(project_id,run_id,physical_invocation_id,call_id,arguments_hash);
CREATE TABLE execution_tool_results (
 project_id uuid NOT NULL,run_id uuid NOT NULL,physical_invocation_id uuid NOT NULL,
 call_id text NOT NULL,arguments_hash text NOT NULL,response_record_id uuid NOT NULL,record_id uuid NOT NULL,
 PRIMARY KEY(project_id,run_id,physical_invocation_id,call_id,arguments_hash),
 FOREIGN KEY(project_id,response_record_id,call_id) REFERENCES execution_provider_calls(project_id,response_record_id,call_id),
 FOREIGN KEY(project_id,record_id) REFERENCES runtime_records(project_id,record_id)
);
CREATE TRIGGER immutable_execution_provider_calls BEFORE UPDATE OR DELETE ON execution_provider_calls
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_execution_tool_results BEFORE UPDATE OR DELETE ON execution_tool_results
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE INDEX execution_event_record_lookup ON evaluation_invocation_events(project_id,record_id);
CREATE INDEX execution_root_request_lookup ON evaluation_model_invocations(project_id,request_record_id);
CREATE INDEX execution_turn_request_lookup ON evaluation_model_turns(project_id,request_record_id);

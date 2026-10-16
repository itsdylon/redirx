-- STAGED: requires the existing062 Jev schema and its mapping-decision prerequisites.
-- Operation keys are owner-scoped; initial confirmations must also identify the run.
-- CREATE OR REPLACE retains the existing owner and ACL. No historical rows change.
BEGIN;
CREATE OR REPLACE FUNCTION public.prepare_jev_pass(p_session_id uuid,p_run_id uuid,p_worker_id text,p_attempt_count integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE j jev_runs%ROWTYPE; r migration_runs%ROWTYPE; pair jsonb; mapping jsonb;
BEGIN
 PERFORM 1 FROM migration_runs WHERE id=p_run_id FOR UPDATE;
 PERFORM lock_migration_engine_attempt(p_session_id,p_run_id,p_worker_id,p_attempt_count);
 SELECT * INTO j FROM jev_runs WHERE run_id=p_run_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found' USING ERRCODE='P0001'; END IF;
 IF NOT j.prepared THEN
  IF j.pass=1 THEN
   SELECT * INTO r FROM migration_runs WHERE id=p_run_id;
   FOR pair IN SELECT value FROM jsonb_array_elements(j.initial_seeds) LOOP
    mapping:=persist_migration_run_mapping(p_session_id,p_run_id,p_worker_id,p_attempt_count,pair->>'old_url',pair->>'new_url',0,'jev_confirmed_seed',true);
    PERFORM resolve_migration_match_decisions(r.user_id,r.migration_id,r.id,r.user_id,
      'jev-initial:'||p_run_id::text||':'||encode(sha256(convert_to(pair::text,'UTF8')),'hex'),
      jsonb_build_array(jsonb_build_object('mapping_id',mapping->>'id','expected_revision',0,'action','set_target','target_url',pair->>'new_url','rationale','Explicit confirmed pair supplied when starting the Jev run.')));
   END LOOP;
  END IF;
  UPDATE jev_runs SET prepared=true,pass_seed_revision=seed_revision,
   seeds=coalesce((SELECT jsonb_agg(jsonb_build_object('old_url',m.old_url,'new_url',d.target_url,'revision',d.revision))
    FROM migration_mapping_decisions d JOIN url_mappings m ON m.id=d.mapping_id
    WHERE d.run_id=p_run_id AND d.action IN('approve','set_target','accept_repair') AND d.target_url IS NOT NULL),'[]')
   WHERE run_id=p_run_id RETURNING * INTO j;
 END IF;
 RETURN to_jsonb(j);
END $$;
COMMIT;

-- Additive current admission; historical billing and provider accounting remain intact.
BEGIN;
ALTER TABLE migration_runs ADD COLUMN current_billing_usage_id uuid UNIQUE REFERENCES redirx_billing_usage(id) ON DELETE CASCADE;
ALTER TABLE redirx_billing_usage ADD CONSTRAINT current_usage_run FOREIGN KEY(run_id) REFERENCES migration_runs(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE jev_runs ADD COLUMN billing_version text, ADD COLUMN billing_sku text;

CREATE FUNCTION validate_current_run_authority(p_run_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE u redirx_billing_usage%ROWTYPE; c redirx_billing_checkouts%ROWTYPE;
BEGIN
 SELECT u1.* INTO u FROM redirx_billing_usage u1 JOIN migration_runs r ON r.current_billing_usage_id=u1.id
 WHERE r.id=p_run_id AND u1.run_id=r.id AND u1.user_id=r.user_id AND u1.migration_id=r.migration_id
 AND u1.old_inventory_id=r.old_inventory_id AND u1.new_inventory_id=r.new_inventory_id;
 IF NOT FOUND OR u.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'payment_required'; END IF;
 IF u.sku='payg' THEN
  SELECT * INTO c FROM redirx_billing_checkouts WHERE id=u.checkout_id;
  IF c.status<>'paid' THEN RAISE EXCEPTION 'payment_required'; END IF;
 END IF;
 IF u.period_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM redirx_billing_periods WHERE id=u.period_id AND status='active') THEN RAISE EXCEPTION 'payment_required'; END IF;
 -- Closing a period prevents new purchases, not use of already-paid three-pass work.
END $$;

CREATE FUNCTION reserve_current_jev_run(p_user_id uuid,p_migration_id uuid,p_old_inventory_id uuid,p_new_inventory_id uuid,
 p_idempotency_key text,p_confirmed_pairs jsonb DEFAULT '[]',p_checkout_id uuid DEFAULT NULL,p_subscription_id uuid DEFAULT NULL,
 p_approve_extra_urls boolean DEFAULT false,p_max_extra_amount_cents integer DEFAULT 0,p_livemode boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE m migration_records%ROWTYPE; c redirx_billing_checkouts%ROWTYPE; b redirx_billing_periods%ROWTYPE;
 counts jsonb; op jsonb; v_result jsonb; pair jsonb; seen text[]:=ARRAY[]::text[]; sku text; extras integer:=0; total_extra integer; amount integer;
 rid uuid:=gen_random_uuid(); sid uuid:=gen_random_uuid(); uid uuid:=gen_random_uuid(); old_urls jsonb; new_urls jsonb;
BEGIN
 IF p_user_id IS NULL OR p_idempotency_key IS NULL OR length(btrim(p_idempotency_key)) NOT BETWEEN 1 AND 200
 OR p_approve_extra_urls IS NULL OR p_max_extra_amount_cents IS NULL OR p_max_extra_amount_cents<0 OR p_livemode IS NULL
 OR (p_checkout_id IS NOT NULL AND p_subscription_id IS NOT NULL)
 OR jsonb_typeof(p_confirmed_pairs) IS DISTINCT FROM 'array' OR jsonb_array_length(p_confirmed_pairs)>100 THEN RAISE EXCEPTION 'invalid_input'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN RAISE EXCEPTION 'not_ready'; END IF;
 PERFORM pg_advisory_xact_lock(630001,hashtext(p_user_id::text));
 SELECT * INTO m FROM migration_records WHERE id=p_migration_id AND user_id=p_user_id FOR NO KEY UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 op:=reserve_migration_operation(p_user_id,m.id,'run_migration',p_idempotency_key,
 encode(sha256(convert_to(jsonb_build_array('redirx_2026_10',p_old_inventory_id,p_new_inventory_id,p_confirmed_pairs,p_checkout_id,p_subscription_id,p_approve_extra_urls,p_max_extra_amount_cents,p_livemode)::text,'UTF8')),'hex'));
 IF (op->>'replayed')::boolean THEN RETURN (op->'result')||jsonb_build_object('replayed',true); END IF;
 counts:=current_inventory_counts(p_user_id,m.id,p_old_inventory_id,p_new_inventory_id);
 FOR pair IN SELECT value FROM jsonb_array_elements(p_confirmed_pairs) LOOP
  IF jsonb_typeof(pair) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(pair))<>2
   OR NOT EXISTS(SELECT 1 FROM session_discovered_urls WHERE inventory_id=p_old_inventory_id AND url=pair->>'old_url')
   OR NOT EXISTS(SELECT 1 FROM session_discovered_urls WHERE inventory_id=p_new_inventory_id AND url=pair->>'new_url') OR (pair->>'old_url')=ANY(seen) THEN RAISE EXCEPTION 'invalid_input'; END IF;
  seen:=array_append(seen,pair->>'old_url');
 END LOOP;
 IF p_checkout_id IS NULL AND p_subscription_id IS NULL THEN
  SELECT bc.id INTO p_checkout_id FROM redirx_billing_checkouts bc
   WHERE bc.user_id=p_user_id AND bc.livemode=p_livemode AND bc.sku='payg' AND bc.status='paid'
    AND bc.migration_id=m.id AND bc.old_inventory_id=p_old_inventory_id AND bc.new_inventory_id=p_new_inventory_id
    AND bc.paid_at+interval '30 days'>clock_timestamp()
    AND NOT EXISTS(SELECT 1 FROM redirx_billing_usage bu WHERE bu.checkout_id=bc.id)
   ORDER BY bc.paid_at,bc.id LIMIT 1;
 END IF;
 IF p_checkout_id IS NULL AND p_subscription_id IS NULL AND (
   (counts->>'old')::integer>500 OR (counts->>'new')::integer>2000 OR (counts->>'bytes')::bigint>2097152
   OR EXISTS(SELECT 1 FROM redirx_billing_trials WHERE user_id=p_user_id)) THEN
  SELECT bc.id INTO p_subscription_id FROM redirx_billing_checkouts bc
   JOIN redirx_billing_periods bp ON bp.checkout_id=bc.id
   WHERE bc.user_id=p_user_id AND bc.livemode=p_livemode AND bc.status='paid' AND bp.status='active'
    AND bp.period_start<=clock_timestamp() AND bp.period_end>clock_timestamp() AND bp.closed_at IS NULL
   ORDER BY bp.period_start DESC LIMIT 1;
 END IF;
 IF p_checkout_id IS NOT NULL THEN
  SELECT * INTO c FROM redirx_billing_checkouts WHERE id=p_checkout_id AND user_id=p_user_id AND livemode=p_livemode FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
  IF c.sku<>'payg' OR c.status<>'paid' OR c.migration_id<>m.id OR c.old_inventory_id<>p_old_inventory_id OR c.new_inventory_id<>p_new_inventory_id
   OR c.paid_at+interval '30 days'<=clock_timestamp() THEN RAISE EXCEPTION 'payment_required'; END IF;
  IF EXISTS(SELECT 1 FROM redirx_billing_usage WHERE checkout_id=c.id) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
  sku:='payg';
 ELSIF p_subscription_id IS NOT NULL THEN
  SELECT * INTO c FROM redirx_billing_checkouts WHERE id=p_subscription_id AND redirx_billing_checkouts.sku IN('studio','agency') AND user_id=p_user_id AND livemode=p_livemode FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
  IF c.status<>'paid' THEN RAISE EXCEPTION 'payment_required'; END IF;
  SELECT * INTO b FROM redirx_billing_periods WHERE checkout_id=c.id AND status='active' AND period_start<=clock_timestamp() AND period_end>clock_timestamp() AND closed_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'payment_required'; END IF;
  total_extra:=greatest(0,b.used_old_urls+(counts->>'old')::integer-b.included_old_urls);
  extras:=total_extra-b.extra_old_urls; amount:=ceil(total_extra::numeric/50)::integer*100;
  IF extras>0 AND (NOT p_approve_extra_urls OR amount>p_max_extra_amount_cents) THEN
   RAISE EXCEPTION 'approval_required' USING DETAIL=jsonb_build_object('extra_old_urls',total_extra,'extra_amount_cents',amount)::text;
  END IF;
  UPDATE redirx_billing_periods SET used_old_urls=used_old_urls+(counts->>'old')::integer,extra_old_urls=total_extra,extra_amount_cents=amount WHERE id=b.id;
  sku:=c.sku;
 ELSE
  IF (counts->>'old')::integer>500 OR (counts->>'new')::integer>2000 OR (counts->>'bytes')::bigint>2097152
   OR EXISTS(SELECT 1 FROM redirx_billing_trials WHERE user_id=p_user_id) THEN RAISE EXCEPTION 'payment_required'; END IF;
  INSERT INTO redirx_billing_trials(user_id) VALUES(p_user_id);
  sku:='trial';
 END IF;
 INSERT INTO redirx_billing_usage(id,user_id,migration_id,old_inventory_id,new_inventory_id,checkout_id,period_id,run_id,sku,livemode,old_urls,extra_urls,approve_extra_urls,max_extra_amount_cents)
 VALUES(uid,p_user_id,m.id,p_old_inventory_id,p_new_inventory_id,c.id,b.id,rid,sku,p_livemode,(counts->>'old')::integer,extras,p_approve_extra_urls,p_max_extra_amount_cents);
 SELECT jsonb_agg(url ORDER BY id) INTO old_urls FROM session_discovered_urls WHERE inventory_id=p_old_inventory_id;
 SELECT jsonb_agg(url ORDER BY id) INTO new_urls FROM session_discovered_urls WHERE inventory_id=p_new_inventory_id;
 INSERT INTO migration_sessions(id,user_id,project_name,status,pipeline_type,is_preview,old_urls,new_urls,idempotency_key,mcp_run_id,priority)
 VALUES(sid,p_user_id::text,m.name,'pending','content',false,old_urls,new_urls,'mcp:'||(op->>'id'),rid,CASE WHEN sku='trial' THEN 0 ELSE 10 END);
 INSERT INTO session_discovered_urls(session_id,side,url,count_key,sources,clicks,impressions)
 SELECT sid,side,url,count_key,sources,clicks,impressions FROM session_discovered_urls WHERE inventory_id IN(p_old_inventory_id,p_new_inventory_id);
 INSERT INTO migration_runs(id,migration_id,user_id,old_inventory_id,new_inventory_id,legacy_session_id,operation_id,current_billing_usage_id)
 VALUES(rid,m.id,p_user_id,p_old_inventory_id,p_new_inventory_id,sid,(op->>'id')::uuid,uid);
 INSERT INTO jev_runs(run_id,initial_seeds,billing_version,billing_sku) VALUES(rid,p_confirmed_pairs,'redirx_2026_10',sku);
 v_result:=jsonb_build_object('migration_id',m.id,'operation_id',op->>'id','run_id',rid,'session_id',sid,'status','queued','replayed',false,
 'billing_version','redirx_2026_10','sku',sku,'free',sku='trial','checkout_id',c.id,'period_id',b.id,'extra_amount_cents',coalesce(amount,0));
 UPDATE migration_operations SET status='queued',result=v_result WHERE id=(op->>'id')::uuid;
 RETURN v_result;
END $$;

CREATE OR REPLACE FUNCTION validate_migration_run_inputs()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
  v_old inventory_snapshots%ROWTYPE;
  v_new inventory_snapshots%ROWTYPE;
  v_old_found BOOLEAN;
  v_new_found BOOLEAN;
  v_legacy_session_user TEXT;
  v_legacy_session_found BOOLEAN;
  v_migration_status TEXT;
  v_migration_found BOOLEAN;
  v_session migration_sessions%ROWTYPE;
  v_quote migration_price_quotes%ROWTYPE;
  v_grant migration_purchase_grants%ROWTYPE;
BEGIN
  IF TG_OP = 'UPDATE' AND (
    NEW.id IS DISTINCT FROM OLD.id
    OR NEW.migration_id IS DISTINCT FROM OLD.migration_id
    OR NEW.user_id IS DISTINCT FROM OLD.user_id
    OR NEW.old_inventory_id IS DISTINCT FROM OLD.old_inventory_id
    OR NEW.new_inventory_id IS DISTINCT FROM OLD.new_inventory_id
    OR NEW.legacy_session_id IS DISTINCT FROM OLD.legacy_session_id
    OR NEW.rerun_of IS DISTINCT FROM OLD.rerun_of
    OR NEW.quote_id IS DISTINCT FROM OLD.quote_id
    OR NEW.grant_id IS DISTINCT FROM OLD.grant_id
    OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
    OR NEW.current_billing_usage_id IS DISTINCT FROM OLD.current_billing_usage_id
    OR NEW.studio_reservation_id IS DISTINCT FROM OLD.studio_reservation_id
  ) THEN
    -- The FK's ON DELETE SET NULL action is the only supported way to detach
    -- a legacy bridge.  Keep the durable run for audit; no caller can use an
    -- ordinary UPDATE to erase the binding.
    IF NEW.id = OLD.id AND NEW.migration_id = OLD.migration_id
       AND NEW.user_id = OLD.user_id
       AND NEW.old_inventory_id IS NOT DISTINCT FROM OLD.old_inventory_id
       AND NEW.new_inventory_id IS NOT DISTINCT FROM OLD.new_inventory_id
       AND NEW.rerun_of IS NOT DISTINCT FROM OLD.rerun_of
       AND NEW.quote_id IS NOT DISTINCT FROM OLD.quote_id
       AND NEW.grant_id IS NOT DISTINCT FROM OLD.grant_id
       AND NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
       AND NEW.current_billing_usage_id IS NOT DISTINCT FROM OLD.current_billing_usage_id
       AND NEW.studio_reservation_id IS NOT DISTINCT FROM OLD.studio_reservation_id
       AND OLD.legacy_session_id IS NOT NULL
       AND NEW.legacy_session_id IS NULL
       AND NOT EXISTS (SELECT 1 FROM migration_sessions WHERE id = OLD.legacy_session_id) THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'migration run bindings are immutable';
  END IF;

  IF NEW.studio_reservation_id IS NOT NULL THEN
    IF NEW.grant_id IS NOT NULL THEN RAISE EXCEPTION 'invalid_input' USING ERRCODE='P0001'; END IF;
    IF NOT EXISTS(SELECT 1 FROM migration_price_quotes WHERE id=NEW.quote_id AND old_inventory_id=NEW.old_inventory_id AND new_inventory_id=NEW.new_inventory_id) THEN RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001'; END IF;
    PERFORM validate_studio_run_authority(NEW.user_id,NEW.migration_id,NEW.quote_id,NEW.operation_id,NEW.studio_reservation_id, TG_OP='INSERT');
  END IF;
  IF NEW.legacy_session_id IS NOT NULL THEN
    SELECT status INTO v_migration_status
      FROM migration_records
      WHERE id = NEW.migration_id AND user_id = NEW.user_id
      FOR KEY SHARE;
    v_migration_found := FOUND;
    SELECT user_id INTO v_legacy_session_user
      FROM migration_sessions WHERE id = NEW.legacy_session_id
      FOR KEY SHARE;
    v_legacy_session_found := FOUND;
    IF NOT v_migration_found OR NOT v_legacy_session_found
       OR v_legacy_session_user <> NEW.user_id::text THEN
      RAISE EXCEPTION 'legacy run bridge does not match its session owner';
    END IF;
    -- Preserve 032's historical bridge. Only the new, explicitly bound path
    -- below can bridge a planned migration to a content session.
    IF v_migration_status = 'legacy_unverified' AND NEW.current_billing_usage_id IS NULL THEN
      IF NEW.quote_id IS NOT NULL OR NEW.grant_id IS NOT NULL OR NEW.operation_id IS NOT NULL OR NEW.studio_reservation_id IS NOT NULL THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE='P0001';
      END IF;
      RETURN NEW;
    END IF;
    SELECT * INTO v_session FROM migration_sessions WHERE id=NEW.legacy_session_id;
    IF NEW.current_billing_usage_id IS NOT NULL THEN
      IF NEW.quote_id IS NOT NULL OR NEW.grant_id IS NOT NULL OR NEW.studio_reservation_id IS NOT NULL
        OR NOT EXISTS(SELECT 1 FROM redirx_billing_usage u WHERE u.id=NEW.current_billing_usage_id AND u.run_id=NEW.id
          AND u.user_id=NEW.user_id AND u.migration_id=NEW.migration_id AND u.old_inventory_id=NEW.old_inventory_id AND u.new_inventory_id=NEW.new_inventory_id)
        OR v_session.mcp_run_id IS DISTINCT FROM NEW.id OR v_session.pipeline_type<>'content' OR v_session.is_preview IS DISTINCT FROM false
        OR NOT EXISTS(SELECT 1 FROM migration_operations WHERE id=NEW.operation_id AND user_id=NEW.user_id AND migration_id=NEW.migration_id AND kind='run_migration')
        OR v_session.old_urls IS DISTINCT FROM (SELECT jsonb_agg(url ORDER BY id) FROM session_discovered_urls WHERE inventory_id=NEW.old_inventory_id)
        OR v_session.new_urls IS DISTINCT FROM (SELECT jsonb_agg(url ORDER BY id) FROM session_discovered_urls WHERE inventory_id=NEW.new_inventory_id)
      THEN RAISE EXCEPTION 'invalid_input'; END IF;
    ELSE
    SELECT * INTO v_quote FROM migration_price_quotes WHERE id=NEW.quote_id
      AND migration_id=NEW.migration_id AND user_id=NEW.user_id;
    SELECT * INTO v_grant FROM migration_purchase_grants WHERE id=NEW.grant_id
      AND migration_id=NEW.migration_id AND user_id=NEW.user_id AND quote_id=NEW.quote_id;
    IF v_session.mcp_run_id IS DISTINCT FROM NEW.id OR v_session.pipeline_type <> 'content'
       OR v_session.is_preview IS DISTINCT FROM false OR v_quote.id IS NULL OR (v_grant.id IS NULL AND NEW.studio_reservation_id IS NULL)
       OR v_quote.old_inventory_id IS DISTINCT FROM NEW.old_inventory_id
       OR v_quote.new_inventory_id IS DISTINCT FROM NEW.new_inventory_id
       OR NOT EXISTS (SELECT 1 FROM migration_operations WHERE id=NEW.operation_id
         AND migration_id=NEW.migration_id AND user_id=NEW.user_id AND kind='run_migration')
       OR v_session.old_urls IS DISTINCT FROM (SELECT jsonb_agg(url ORDER BY id) FROM session_discovered_urls WHERE inventory_id=NEW.old_inventory_id)
       OR v_session.new_urls IS DISTINCT FROM (SELECT jsonb_agg(url ORDER BY id) FROM session_discovered_urls WHERE inventory_id=NEW.new_inventory_id) THEN
      RAISE EXCEPTION 'invalid_input' USING ERRCODE='P0001';
    END IF;
  END IF;

  END IF; -- historical quote branch

  SELECT * INTO v_old FROM inventory_snapshots
    WHERE id = NEW.old_inventory_id
      AND migration_id = NEW.migration_id
      AND user_id = NEW.user_id
    FOR UPDATE;
  v_old_found := FOUND;
  SELECT * INTO v_new FROM inventory_snapshots
    WHERE id = NEW.new_inventory_id
      AND migration_id = NEW.migration_id
      AND user_id = NEW.user_id
    FOR UPDATE;
  v_new_found := FOUND;
  IF NOT v_old_found OR NOT v_new_found
     OR v_old.side <> 'old' OR v_new.side <> 'new'
     OR v_old.status <> 'complete' OR v_new.status <> 'complete' THEN
    RAISE EXCEPTION 'new runs require complete old and new inventory snapshots';
  END IF;
  RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION authorize_migration_run_dispatch(
 p_session_id uuid,p_run_id uuid,p_worker_id text,p_attempt_count integer,p_activation text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE s migration_sessions%ROWTYPE; r migration_runs%ROWTYPE;
BEGIN
 IF p_activation IS NULL OR p_activation NOT IN('test_only','redirx_2026_10') THEN RAISE EXCEPTION 'not_ready' USING ERRCODE='P0001'; END IF;
 SELECT * INTO s FROM migration_sessions WHERE id=p_session_id AND mcp_run_id=p_run_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found' USING ERRCODE='P0001'; END IF;
 IF s.status<>'processing' OR s.locked_by IS DISTINCT FROM p_worker_id OR s.attempt_count IS DISTINCT FROM p_attempt_count
  OR s.lease_expires_at IS NULL OR s.lease_expires_at<=now() THEN
  RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001'; END IF;
 SELECT * INTO r FROM migration_runs WHERE id=p_run_id AND legacy_session_id=s.id AND user_id::text=s.user_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found' USING ERRCODE='P0001'; END IF;
 IF (r.current_billing_usage_id IS NOT NULL) IS DISTINCT FROM (p_activation='redirx_2026_10') THEN RAISE EXCEPTION 'not_ready'; END IF;
 IF r.current_billing_usage_id IS NOT NULL THEN
  PERFORM validate_current_run_authority(r.id);
 ELSIF r.studio_reservation_id IS NOT NULL THEN
  PERFORM validate_studio_run_authority(r.user_id,r.migration_id,r.quote_id,r.operation_id,r.studio_reservation_id,true);
 ELSE
  PERFORM validate_migration_run_grant(r.user_id,r.migration_id,r.quote_id,r.old_inventory_id,r.new_inventory_id,r.grant_id);
 END IF;
 UPDATE migration_runs SET authorized_attempt=p_attempt_count,dispatch_authorized_at=now() WHERE id=r.id;
 UPDATE migration_operations SET status='running' WHERE id=r.operation_id;
 RETURN jsonb_build_object('run_id',r.id,'session_id',s.id,'operation_id',r.operation_id,'migration_id',r.migration_id,'status','running');
END $$;
CREATE OR REPLACE FUNCTION refine_jev_run(p_user_id uuid,p_migration_id uuid,p_run_id uuid,p_expected_seed_revision integer,p_idempotency_key text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE r migration_runs%ROWTYPE; s migration_sessions%ROWTYPE; j jev_runs%ROWTYPE; op jsonb; answer jsonb;
BEGIN
 IF p_idempotency_key IS NULL OR length(p_idempotency_key) NOT BETWEEN 1 AND 200 THEN
  RAISE EXCEPTION 'invalid_input' USING ERRCODE='P0001'; END IF;
 SELECT * INTO r FROM migration_runs WHERE id=p_run_id AND migration_id=p_migration_id AND user_id=p_user_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found' USING ERRCODE='P0001'; END IF;
 SELECT * INTO s FROM migration_sessions WHERE id=r.legacy_session_id FOR UPDATE;
 SELECT * INTO j FROM jev_runs WHERE run_id=p_run_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found' USING ERRCODE='P0001'; END IF;
 op:=reserve_migration_operation(p_user_id,p_migration_id,'resolve_matches','jev-refine:'||encode(sha256(convert_to(p_idempotency_key,'UTF8')),'hex'),
  encode(sha256(convert_to(jsonb_build_object('run',p_run_id,'seed_revision',p_expected_seed_revision)::text,'UTF8')),'hex'));
 IF (op->>'replayed')::boolean THEN RETURN (op->'result')||jsonb_build_object('replayed',true); END IF;
 IF r.current_billing_usage_id IS NOT NULL THEN PERFORM validate_current_run_authority(r.id); END IF;
 IF p_expected_seed_revision IS DISTINCT FROM j.seed_revision OR s.status NOT IN('completed','permanently_failed') THEN
  RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001'; END IF;
 IF s.status='completed' THEN
  IF j.pass>=3 OR j.seed_revision=j.pass_seed_revision THEN RAISE EXCEPTION 'refinement_limit' USING ERRCODE='P0001'; END IF;
  UPDATE jev_runs SET pass=pass+1,prepared=false WHERE run_id=p_run_id;
 END IF;
 -- Attempt generations must never repeat on the same session/worker tuple.
 -- Old in-flight cache/budget writes remain fenced after explicit resume.
 UPDATE migration_sessions SET status='pending',started_at=NULL,completed_at=NULL,
  locked_by=NULL,locked_at=NULL,lease_expires_at=NULL,last_error=NULL WHERE id=s.id;
 UPDATE migration_operations SET status='queued' WHERE id=r.operation_id;
 answer:=jsonb_build_object('migration_id',p_migration_id,'run_id',p_run_id,'operation_id',r.operation_id,'status','queued','replayed',false);
 UPDATE migration_operations SET status='succeeded',result=answer WHERE id=(op->>'id')::uuid;
 RETURN answer;
END $$;
REVOKE ALL ON FUNCTION validate_current_run_authority(uuid),reserve_current_jev_run(uuid,uuid,uuid,uuid,text,jsonb,uuid,uuid,boolean,integer,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION validate_current_run_authority(uuid),reserve_current_jev_run(uuid,uuid,uuid,uuid,text,jsonb,uuid,uuid,boolean,integer,boolean) TO service_role;
CREATE OR REPLACE FUNCTION reserve_jev_provider_call(p_session_id uuid,p_run_id uuid,p_worker_id text,p_attempt_count integer,p_daily_limit integer,p_reservation_micro_usd integer,p_budget_micro_usd integer)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE reservation uuid; used integer; day_used integer; spent bigint; today date := (clock_timestamp() AT TIME ZONE 'UTC')::date;
BEGIN
 PERFORM 1 FROM migration_runs WHERE id=p_run_id FOR UPDATE;
 PERFORM lock_migration_engine_attempt(p_session_id,p_run_id,p_worker_id,p_attempt_count);
 IF p_daily_limit NOT BETWEEN 1 AND 10000 OR p_reservation_micro_usd NOT BETWEEN 1 AND 100000 OR p_budget_micro_usd NOT BETWEEN 1 AND 1000000 THEN RAISE EXCEPTION 'invalid_input' USING ERRCODE='P0001'; END IF;
 SELECT provider_requests INTO used FROM jev_runs WHERE run_id=p_run_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found' USING ERRCODE='P0001'; END IF;
 INSERT INTO jev_provider_daily_budget(day) VALUES(today) ON CONFLICT DO NOTHING;
 SELECT requests,reserved_micro_usd INTO day_used,spent FROM jev_provider_daily_budget WHERE day=today FOR UPDATE;
 IF used>=(CASE WHEN EXISTS(SELECT 1 FROM jev_runs WHERE run_id=p_run_id AND billing_version='redirx_2026_10' AND billing_sku IN('payg','studio','agency')) THEN 100000 ELSE 4000 END) OR day_used>=p_daily_limit OR spent+p_reservation_micro_usd>p_budget_micro_usd THEN RAISE EXCEPTION 'provider_budget_exhausted' USING ERRCODE='P0001'; END IF;
 UPDATE jev_runs SET provider_requests=provider_requests+1 WHERE run_id=p_run_id;
 UPDATE jev_provider_daily_budget SET requests=requests+1,reserved_micro_usd=reserved_micro_usd+p_reservation_micro_usd WHERE day=today;
 INSERT INTO jev_provider_reservations(run_id,day,reserved_micro_usd) VALUES(p_run_id,today,p_reservation_micro_usd) RETURNING id INTO reservation;
 RETURN reservation;
END $$;
CREATE FUNCTION current_run_export_authority(p_user_id uuid,p_migration_id uuid,p_run_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE u redirx_billing_usage%ROWTYPE;
BEGIN
 SELECT bu.* INTO u FROM redirx_billing_usage bu JOIN migration_runs r ON r.current_billing_usage_id=bu.id AND r.id=bu.run_id
 JOIN migration_sessions ms ON ms.id=r.legacy_session_id AND ms.mcp_run_id=r.id AND ms.user_id=r.user_id::text
 WHERE r.id=p_run_id AND r.user_id=p_user_id AND r.migration_id=p_migration_id AND bu.user_id=r.user_id
 AND bu.migration_id=r.migration_id AND bu.old_inventory_id=r.old_inventory_id AND bu.new_inventory_id=r.new_inventory_id
 AND ms.status='completed' AND ms.attempt_count=r.authorized_attempt;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 -- Time, ordinary subscription lapse, and execution suspension never erase
 -- ownership of completed output or the ability to manually review/export it.
 RETURN jsonb_build_object('eligible',true,'authority','current_billing','state','succeeded','run_id',u.run_id,'usage_id',u.id,'sku',u.sku);
END $$;
REVOKE ALL ON FUNCTION current_run_export_authority(uuid,uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION current_run_export_authority(uuid,uuid,uuid) TO service_role;

CREATE OR REPLACE FUNCTION publish_migration_artifact(
  p_user_id UUID, p_migration_id UUID, p_run_id UUID,
  p_idempotency_key TEXT, p_artifact JSONB, p_content TEXT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp AS $$
DECLARE
  v_existing migration_artifact_mutations%ROWTYPE;
  v_hash TEXT;
  v_artifact UUID := gen_random_uuid();
  v_result JSONB;
  v_run migration_runs%ROWTYPE;
  v_run_json JSONB;
  v_revision INTEGER;
  v_session UUID;
  v_item JSONB;
  v_mapping UUID;
  v_current_old TEXT;
  v_current_new TEXT;
  v_current_action TEXT;
  v_studio_authority JSONB;
  v_studio_authority_row BOOLEAN;
  v_grant_authority BOOLEAN;
BEGIN
  IF p_user_id IS NULL OR p_migration_id IS NULL OR p_run_id IS NULL
     OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = ''
     OR jsonb_typeof(p_artifact) <> 'object' OR p_content IS NULL THEN
    RAISE EXCEPTION 'invalid_input' USING ERRCODE = 'P0001';
  END IF;
  v_hash := encode(sha256(convert_to(p_content, 'UTF8')), 'hex');
  IF p_artifact->>'content_hash' IS DISTINCT FROM v_hash
     OR jsonb_typeof(p_artifact->'verification_inputs') <> 'object'
     OR jsonb_typeof(p_artifact->'verification_inputs'->'redirects') <> 'array'
     OR p_artifact->'verification_inputs'->>'artifact_content_hash' IS DISTINCT FROM v_hash
     OR p_artifact->>'included_count' IS NULL OR p_artifact->>'excluded_count' IS NULL
     OR (p_artifact->>'included_count')::integer <> jsonb_array_length(p_artifact->'verification_inputs'->'redirects')
     OR jsonb_typeof(p_artifact->'target_origins') <> 'array'
     OR jsonb_typeof(p_artifact->'exclusion_reasons') <> 'object' THEN
    RAISE EXCEPTION 'invalid_input' USING ERRCODE = 'P0001';
  END IF;
  -- Serialize publication with 051's selection_revision trigger.  KEY SHARE
  -- does not conflict with that non-key UPDATE; NO KEY UPDATE does.  The
  -- decision RPC takes the run lock before mapping locks; publication takes
  -- only this run lock and then performs non-locking mapping reads, so it does
  -- not create a reverse run/mapping lock order.
  PERFORM 1 FROM migration_runs WHERE id = p_run_id AND migration_id = p_migration_id AND user_id = p_user_id FOR NO KEY UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO v_run FROM migration_runs
    WHERE id = p_run_id AND migration_id = p_migration_id AND user_id = p_user_id FOR NO KEY UPDATE;
  v_run_json := to_jsonb(v_run);
  IF v_run_json->>'legacy_session_id' IS NULL
     OR v_run_json->>'operation_id' IS NULL
     OR v_run_json->>'authorized_attempt' IS NULL THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
  END IF;
  v_session := (v_run_json->>'legacy_session_id')::uuid;
  IF NOT EXISTS (SELECT 1 FROM migration_sessions
      WHERE id=v_session AND user_id=p_user_id::text AND mcp_run_id=p_run_id
        AND status='completed' AND attempt_count=(v_run_json->>'authorized_attempt')::integer) THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
  END IF;
  IF v_run_json->>'grant_id' IS NULL AND v_run_json->>'studio_reservation_id' IS NULL AND v_run_json->>'current_billing_usage_id' IS NULL THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
  END IF;
  IF v_run_json->>'current_billing_usage_id' IS NOT NULL THEN
    PERFORM current_run_export_authority(p_user_id,p_migration_id,p_run_id);
  END IF;
  IF v_run_json->>'grant_id' IS NOT NULL THEN
    SELECT true INTO v_grant_authority FROM migration_purchase_grants g
      WHERE g.id=(v_run_json->>'grant_id')::uuid AND g.user_id=p_user_id
        AND g.migration_id=p_migration_id AND g.quote_id=(v_run_json->>'quote_id')::uuid
        AND g.state='active' FOR SHARE;
    IF v_grant_authority IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  IF v_run_json->>'studio_reservation_id' IS NOT NULL THEN
    BEGIN
      EXECUTE 'SELECT studio_run_entitlement($1::uuid,$2::uuid,$3::uuid)'
        INTO v_studio_authority
        USING p_user_id, p_migration_id, p_run_id;
    EXCEPTION WHEN undefined_function THEN
      RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
    END;
    IF jsonb_typeof(v_studio_authority) <> 'object'
       OR v_studio_authority->>'eligible' IS DISTINCT FROM 'true'
       OR v_studio_authority->>'reservation_id' IS DISTINCT FROM v_run_json->>'studio_reservation_id'
       OR v_studio_authority->>'quote_id' IS DISTINCT FROM v_run_json->>'quote_id'
       OR v_studio_authority->>'operation_id' IS DISTINCT FROM v_run_json->>'operation_id' THEN
      RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
    END IF;
    -- 046 is optional at migration-install time.  When its column is bound,
    -- dynamic SQL keeps 041 installable before the Studio tables exist.
    EXECUTE $q$
      SELECT true FROM migration_studio_work_reservations w
      JOIN migration_studio_slots s ON s.id=w.slot_id AND s.migration_id=w.migration_id AND s.user_id=w.user_id
      JOIN migration_test_subscriptions sub ON sub.id=s.subscription_id AND sub.user_id=s.user_id
      WHERE w.id=$1::uuid AND w.user_id=$2::uuid AND w.migration_id=$3::uuid
        AND w.quote_id=$4::uuid AND w.run_operation_id=$5::uuid
        AND w.state='succeeded' AND s.state='completed' AND s.first_success_at IS NOT NULL
        AND sub.status <> 'revoked'
      FOR SHARE OF w, s, sub
    $q$
    INTO v_studio_authority_row
    USING (v_run_json->>'studio_reservation_id'), p_user_id, p_migration_id,
      v_run_json->>'quote_id', v_run_json->>'operation_id';
    IF v_studio_authority_row IS DISTINCT FROM true THEN
      RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
    END IF;
  END IF;

  -- Idempotent replay is bound to the original artifact request hash.  Check
  -- it after current ownership/entitlement checks but before the current
  -- selection revision: a later decision edit must not make an immutable,
  -- exact replay disappear.
  SELECT * INTO v_existing FROM migration_artifact_mutations
    WHERE user_id = p_user_id AND migration_id = p_migration_id AND kind = 'artifact'
      AND idempotency_key = p_idempotency_key FOR UPDATE;
  IF FOUND THEN
    IF v_existing.request_hash <> encode(sha256(convert_to(p_artifact::text || p_content, 'UTF8')), 'hex')
       THEN RAISE EXCEPTION 'operation_conflict' USING ERRCODE = 'P0001'; END IF;
    RETURN v_existing.result || jsonb_build_object('replayed', true);
  END IF;

  BEGIN
    v_revision := (p_artifact->>'decision_revision')::integer;
  EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'invalid_input' USING ERRCODE = 'P0001';
  END;
  IF v_run_json->>'selection_revision' IS DISTINCT FROM v_revision::text THEN
    RAISE EXCEPTION 'operation_conflict' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM migration_mapping_decisions
      WHERE run_id=p_run_id AND migration_id=p_migration_id AND user_id=p_user_id
        AND revision > v_revision) THEN
    RAISE EXCEPTION 'operation_conflict' USING ERRCODE = 'P0001';
  END IF;
  FOR v_item IN SELECT value FROM jsonb_array_elements(p_artifact->'verification_inputs'->'redirects') LOOP
    BEGIN v_mapping := (v_item->>'mapping_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN RAISE EXCEPTION 'invalid_input' USING ERRCODE = 'P0001'; END;
    SELECT m.old_url, COALESCE(d.target_url,m.new_url), d.action INTO v_current_old,v_current_new,v_current_action
      FROM url_mappings m LEFT JOIN migration_mapping_decisions d ON d.run_id=p_run_id AND d.mapping_id=m.id
      WHERE m.id=v_mapping AND m.session_id=v_session;
    IF NOT FOUND OR v_item->>'source_url' IS DISTINCT FROM v_current_old
       OR v_item->>'expected_url' IS DISTINCT FROM v_current_new
       OR v_current_action IN ('reject','defer','intentional_removal') THEN
      RAISE EXCEPTION 'operation_conflict' USING ERRCODE = 'P0001';
    END IF;
  END LOOP;
  v_result := jsonb_build_object(
    'id', v_artifact, 'migration_id', p_migration_id, 'user_id', p_user_id,
    'run_id', p_run_id, 'decision_revision', p_artifact->>'decision_revision',
    'format', p_artifact->>'format', 'content_hash', v_hash,
    'storage_key', 'db://migration_artifact_contents/' || v_artifact,
    'target_origins', p_artifact->'target_origins', 'included_count', (p_artifact->>'included_count')::integer,
    'excluded_count', (p_artifact->>'excluded_count')::integer,
    'partial_policy', p_artifact->>'partial_policy', 'verification_inputs', p_artifact->'verification_inputs',
    'replayed', false);
  INSERT INTO migration_artifacts (id, migration_id, user_id, run_id, decision_revision, format,
    content_hash, storage_key, target_origins, included_count, excluded_count, exclusion_reasons,
    destination_mapping, verification_inputs, partial_policy)
  VALUES (v_artifact, p_migration_id, p_user_id, p_run_id, p_artifact->>'decision_revision',
    p_artifact->>'format', v_hash, 'db://migration_artifact_contents/' || v_artifact,
    p_artifact->'target_origins', (p_artifact->>'included_count')::integer, (p_artifact->>'excluded_count')::integer,
    p_artifact->'exclusion_reasons', p_artifact->'destination_mapping', p_artifact->'verification_inputs', p_artifact->>'partial_policy');
  INSERT INTO migration_artifact_contents (artifact_id, migration_id, user_id, content, content_hash, format)
    VALUES (v_artifact, p_migration_id, p_user_id, p_content, v_hash, p_artifact->>'format');
  INSERT INTO migration_artifact_mutations (user_id, migration_id, kind, idempotency_key, request_hash, artifact_id, result)
    VALUES (p_user_id, p_migration_id, 'artifact', p_idempotency_key,
      encode(sha256(convert_to(p_artifact::text || p_content, 'UTF8')), 'hex'), v_artifact, v_result);
  RETURN v_result;
EXCEPTION WHEN unique_violation THEN
  SELECT * INTO v_existing FROM migration_artifact_mutations WHERE user_id = p_user_id AND migration_id = p_migration_id AND kind = 'artifact' AND idempotency_key = p_idempotency_key;
  IF FOUND THEN RETURN v_existing.result || jsonb_build_object('replayed', true); END IF;
  RAISE EXCEPTION 'operation_conflict' USING ERRCODE = 'P0001';
END;
$$;


CREATE OR REPLACE FUNCTION public.cleanup_deleted_pivot_migration_engine()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $function$
BEGIN
 -- Audited decisions restrict mapping deletion. The parent is already gone,
 -- so their existing immutable-event trigger permits this exact cascade.
 DELETE FROM public.migration_mapping_decisions
 WHERE migration_id=OLD.id AND user_id=OLD.user_id;
 -- Parent row is already gone, but its runs/sessions still exist. Remove only
 -- the bound pivot children before the ordinary run -> session FK cascade.
 DELETE FROM public.url_mappings child USING public.migration_sessions s,public.migration_runs r
 WHERE child.session_id=s.id AND s.mcp_run_id=r.id AND r.legacy_session_id=s.id
  AND r.migration_id=OLD.id AND r.user_id=OLD.user_id AND s.user_id=OLD.user_id::text;
 DELETE FROM public.webpage_embeddings child USING public.migration_sessions s,public.migration_runs r
 WHERE child.session_id=s.id AND s.mcp_run_id=r.id AND r.legacy_session_id=s.id
  AND r.migration_id=OLD.id AND r.user_id=OLD.user_id AND s.user_id=OLD.user_id::text;
 RETURN OLD;
END $function$;

COMMIT;

-- Historical browser disconnect shares agent account/credential authority.
-- Requires 024 and 040. Apply before the corresponding browser service release;
-- deferred billing 059–061 are not prerequisites. No provider calls occur in SQL.
BEGIN;
CREATE FUNCTION public.disconnect_legacy_gsc_connection(p_user_id UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE account public.gsc_agent_accounts; next_epoch BIGINT; removed_token TEXT; removed_count INTEGER;
BEGIN
 IF p_user_id IS NULL THEN RAISE EXCEPTION 'invalid_input' USING ERRCODE='P0001'; END IF;
 -- Same account-first lock order as reserve/finish_gsc_agent_operation in 040.
 INSERT INTO public.gsc_agent_accounts(user_id) VALUES(p_user_id) ON CONFLICT DO NOTHING;
 SELECT * INTO account FROM public.gsc_agent_accounts WHERE user_id=p_user_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001'; END IF;

 DELETE FROM public.gsc_connections WHERE user_id=p_user_id::text RETURNING refresh_token INTO removed_token;
 GET DIAGNOSTICS removed_count=ROW_COUNT;
 -- A suppressing trigger must not turn a retained credential into success.
 IF EXISTS(SELECT 1 FROM public.gsc_connections WHERE user_id=p_user_id::text) THEN
  RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001';
 END IF;
 UPDATE public.gsc_agent_accounts SET epoch=epoch+1,state='disconnected'
 WHERE user_id=p_user_id RETURNING epoch INTO next_epoch;
 IF NOT FOUND OR next_epoch<>account.epoch+1 OR NOT EXISTS(
  SELECT 1 FROM public.gsc_agent_accounts WHERE user_id=p_user_id AND state='disconnected' AND epoch=next_epoch
 ) THEN RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001'; END IF;

 -- Sensitive token is for this server's best-effort form-body revoke only.
 -- Never forward the receipt to a browser, agent, telemetry or an error log.
 RETURN jsonb_build_object('user_id',p_user_id,'disconnected',true,'epoch',next_epoch,
  'deleted_count',removed_count,'revocation_token',removed_token);
END $$;
REVOKE ALL ON FUNCTION public.disconnect_legacy_gsc_connection(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.disconnect_legacy_gsc_connection(UUID) TO service_role;
COMMIT;

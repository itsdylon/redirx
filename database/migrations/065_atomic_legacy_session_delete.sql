-- Historical session deletion must commit or roll back as one operation.
-- Apply after062; deferred billing059-061 are not prerequisites.
-- Existing pivot evidence guards remain authoritative; this is no cleanup bypass.
BEGIN;
CREATE FUNCTION public.delete_owned_legacy_session(p_user_id uuid,p_session_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE owned public.migration_sessions%ROWTYPE; ids uuid[]; item record;
BEGIN
 SELECT * INTO owned FROM public.migration_sessions WHERE id=p_session_id FOR UPDATE;
 IF NOT FOUND OR owned.user_id IS DISTINCT FROM p_user_id::text THEN
  RAISE EXCEPTION 'not_found' USING ERRCODE='P0001';
 END IF;
 -- Include preview descendants, including historical nested previews. UNION
 -- terminates malformed cycles. Lock before deletion; FK checks fence new children.
 ids:=ARRAY[]::uuid[];
 FOR item IN
  WITH RECURSIVE descendants(id) AS (
   SELECT p_session_id UNION
   SELECT s.id FROM public.migration_sessions s JOIN descendants d ON s.source_session_id=d.id
  )
  SELECT s.id,s.user_id,s.mcp_run_id FROM public.migration_sessions s
  JOIN descendants d ON d.id=s.id ORDER BY s.id FOR UPDATE OF s
 LOOP
  IF item.user_id IS DISTINCT FROM p_user_id::text OR item.mcp_run_id IS NOT NULL THEN
   RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001';
  END IF;
  ids:=array_append(ids,item.id);
 END LOOP;
 -- A child may have committed while an earlier descendant lock was awaited.
 -- Re-read after every discovered parent is locked; abort instead of allowing
 -- an uninspected descendant to disappear through a cascading foreign key.
 IF EXISTS (
  WITH RECURSIVE descendants(id) AS (
   SELECT p_session_id UNION
   SELECT s.id FROM public.migration_sessions s JOIN descendants d ON s.source_session_id=d.id
  ) SELECT 1 FROM descendants WHERE NOT(id=ANY(ids))
 ) THEN RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001'; END IF;
 DELETE FROM public.url_mappings WHERE session_id=ANY(ids);
 DELETE FROM public.webpage_embeddings WHERE session_id=ANY(ids);
 DELETE FROM public.migration_sessions WHERE id=ANY(ids);
 IF EXISTS(SELECT 1 FROM public.migration_sessions WHERE id=ANY(ids)) THEN
  RAISE EXCEPTION 'operation_conflict' USING ERRCODE='P0001';
 END IF;
 RETURN jsonb_build_object('session_id',p_session_id,'deleted_count',cardinality(ids));
END $$;
REVOKE ALL ON FUNCTION public.delete_owned_legacy_session(uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.delete_owned_legacy_session(uuid,uuid) TO service_role;
COMMIT;

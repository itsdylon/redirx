-- Explicit cursor mode for historical browser lists. Apply before the new reader.
-- Counts and one bounded page share a statement snapshot; separate requests are live.
BEGIN;
CREATE FUNCTION public.legacy_session_page(p_user_id UUID,p_search TEXT DEFAULT '',
 p_after_created_at TIMESTAMPTZ DEFAULT NULL,p_after_id UUID DEFAULT NULL,p_limit INTEGER DEFAULT 50)
RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
 IF p_user_id IS NULL OR p_search IS NULL OR length(p_search)>200 OR p_limit IS NULL
  OR p_limit NOT BETWEEN 1 AND 100 OR (p_after_id IS NULL AND p_after_created_at IS NOT NULL) THEN
  RAISE EXCEPTION 'invalid_input' USING ERRCODE='P0001';
 END IF;
 RETURN (
 WITH eligible AS MATERIALIZED (
  SELECT id,project_name,created_at,coalesce(total_mappings,0) AS total_mappings,
   coalesce(approved_mappings,0) AS approved_mappings,status,pipeline_type,source_session_id
  FROM migration_sessions
  WHERE user_id=p_user_id::text AND is_tutorial=false AND is_preview=false
   AND (p_search='' OR strpos(lower(coalesce(nullif(project_name,''),'Untitled Session')),lower(p_search))>0
    OR strpos(lower(coalesce(status,'')),lower(p_search))>0
    OR strpos(lower(coalesce(pipeline_type,'')),lower(p_search))>0)
 ), page AS MATERIALIZED (
  SELECT * FROM eligible
  WHERE p_after_id IS NULL
   OR (p_after_created_at IS NULL AND (created_at IS NOT NULL OR (created_at IS NULL AND id<p_after_id)))
   OR (p_after_created_at IS NOT NULL AND created_at IS NOT NULL AND (created_at,id)<(p_after_created_at,p_after_id))
  ORDER BY created_at DESC NULLS FIRST,id DESC LIMIT p_limit+1
 ), visible AS (
  SELECT * FROM page ORDER BY created_at DESC NULLS FIRST,id DESC LIMIT p_limit
 )
 SELECT jsonb_build_object('user_id',p_user_id,'search',p_search,
  'total_count',(SELECT count(*) FROM eligible),
  'sessions',coalesce((SELECT jsonb_agg(to_jsonb(visible) ORDER BY created_at DESC NULLS FIRST,id DESC) FROM visible),'[]'::jsonb),
  'next_after',CASE WHEN (SELECT count(*) FROM page)>p_limit THEN (
    SELECT jsonb_build_object('created_at',created_at,'id',id) FROM visible
    ORDER BY created_at ASC NULLS LAST,id ASC LIMIT 1) ELSE NULL END)
 );
END $$;
REVOKE ALL ON FUNCTION public.legacy_session_page(UUID,TEXT,TIMESTAMPTZ,UUID,INTEGER) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.legacy_session_page(UUID,TEXT,TIMESTAMPTZ,UUID,INTEGER) TO service_role;
COMMIT;

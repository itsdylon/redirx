-- Historical dashboard: one owner-scoped snapshot, independent of PostgREST row caps.
-- Apply before the application calls legacy_dashboard_snapshot. No billing or
-- policy changes: exclude tutorials/previews; MCP-linked sessions remain included.
BEGIN;
CREATE FUNCTION legacy_dashboard_snapshot(p_user_id UUID)
RETURNS JSONB LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp AS $$
 WITH sessions AS MATERIALIZED (
  SELECT id,project_name,created_at,coalesce(total_mappings,0) AS total_mappings,
   coalesce(approved_mappings,0) AS approved_mappings,status,pipeline_type,source_session_id
  FROM migration_sessions
  WHERE user_id=p_user_id::text AND is_tutorial=false AND is_preview=false
 ), totals AS (
  SELECT count(*) AS total_sessions,coalesce(sum(total_mappings),0) AS total_redirects,
   coalesce(sum(approved_mappings),0) AS total_approved FROM sessions
 ), confidence AS (
  SELECT count(*) FILTER(WHERE m.match_type='exact_url') AS exact,
   count(*) FILTER(WHERE m.match_type IS DISTINCT FROM 'exact_url') AS count,
   coalesce(sum(coalesce(m.confidence_score,0)) FILTER(WHERE m.match_type IS DISTINCT FROM 'exact_url'),0) AS sum,
   count(*) FILTER(WHERE m.match_type IS DISTINCT FROM 'exact_url' AND coalesce(m.confidence_score,0)>=0.85) AS high,
   count(*) FILTER(WHERE m.match_type IS DISTINCT FROM 'exact_url' AND coalesce(m.confidence_score,0)>=0.65 AND coalesce(m.confidence_score,0)<0.85) AS medium,
   count(*) FILTER(WHERE m.match_type IS DISTINCT FROM 'exact_url' AND coalesce(m.confidence_score,0)<0.65) AS low
  FROM url_mappings m JOIN sessions s ON s.id=m.session_id
 ), recent AS (
  SELECT * FROM sessions ORDER BY created_at DESC,id DESC LIMIT 5
 )
 SELECT jsonb_build_object('user_id',p_user_id,'total_sessions',totals.total_sessions,
  'total_redirects',totals.total_redirects,'total_approved',totals.total_approved,
  'confidence_sum',confidence.sum,'confidence_count',confidence.count,
  'confidence_breakdown',jsonb_build_object('exact',confidence.exact,'high',confidence.high,'medium',confidence.medium,'low',confidence.low),
  'recent_sessions',coalesce((SELECT jsonb_agg(to_jsonb(recent) ORDER BY created_at DESC,id DESC) FROM recent),'[]'::jsonb))
 FROM totals CROSS JOIN confidence;
$$;
REVOKE ALL ON FUNCTION legacy_dashboard_snapshot(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION legacy_dashboard_snapshot(UUID) TO service_role;
COMMIT;

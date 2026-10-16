"""Validate one aggregate read while preserving the browser dashboard contract."""
import math


def read_dashboard(client, owner):
    response = client.rpc('legacy_dashboard_snapshot', {'p_user_id': owner}).execute()
    data = getattr(response, 'data', None)
    if getattr(response, 'error', None) or not isinstance(data, dict) or data.get('user_id') != owner:
        raise ValueError('Dashboard snapshot unavailable')
    for key in ('total_sessions', 'total_redirects', 'total_approved', 'confidence_count'):
        if type(data.get(key)) is not int or data[key] < 0:
            raise ValueError('Dashboard aggregate incomplete')
    breakdown = data.get('confidence_breakdown')
    if not isinstance(breakdown, dict) or any(type(breakdown.get(key)) is not int or breakdown[key] < 0
                                            for key in ('high', 'medium', 'low', 'exact')):
        raise ValueError('Dashboard confidence incomplete')
    if sum(breakdown[key] for key in ('high', 'medium', 'low')) != data['confidence_count']:
        raise ValueError('Dashboard confidence inconsistent')
    score_sum = data.get('confidence_sum')
    if type(score_sum) not in (int, float) or not math.isfinite(score_sum):
        raise ValueError('Dashboard confidence incomplete')
    recent = data.get('recent_sessions')
    if not isinstance(recent, list) or len(recent) != min(5, data['total_sessions']):
        raise ValueError('Dashboard recent sessions incomplete')
    fields = {'id', 'project_name', 'created_at', 'total_mappings', 'approved_mappings',
              'status', 'pipeline_type', 'source_session_id'}
    if any(not isinstance(row, dict) or not fields.issubset(row) for row in recent):
        raise ValueError('Dashboard recent session incomplete')
    total, count = data['total_redirects'], data['confidence_count']
    return {'success': True, 'total_redirects': total, 'total_sessions': data['total_sessions'],
            'approval_progress': round(data['total_approved'] / total * 100, 1) if total else 0,
            'average_confidence': round(score_sum / count * 100, 1) if count else 0,
            'confidence_breakdown': breakdown, 'recent_sessions': recent}

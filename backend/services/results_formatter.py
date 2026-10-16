"""
Data transformation utilities for converting database records to frontend format.
"""
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from uuid import UUID


def calculate_confidence_band(score: float) -> str:
    """
    Map confidence score (0.0-1.0) to confidence band.

    Args:
        score: Confidence score between 0.0 and 1.0

    Returns:
        "high", "medium", or "low"
    """
    if score >= 0.85:
        return "high"
    elif score >= 0.65:
        return "medium"
    else:
        return "low"


def derive_warnings(mapping: Dict[str, Any]) -> List[str]:
    """
    Generate warning array based on mapping properties.

    Args:
        mapping: Database mapping record

    Returns:
        List of warning strings
    """
    warnings = []

    # Add warning if needs review
    if mapping.get('needs_review', False):
        warnings.append('needs-review')

    # Add warning based on match type
    match_type = mapping.get('match_type', '')
    confidence = mapping.get('confidence_score', 0.0)

    # Add near-tie warning for medium confidence matches
    if 0.65 <= confidence < 0.85:
        warnings.append('near-tie')

    # Add low-confidence warning
    if confidence < 0.65:
        warnings.append('low-confidence')

    return warnings


def calculate_path_similarity(old_url: str, new_url: str) -> int:
    """
    Calculate similarity between URL paths using SequenceMatcher.

    Args:
        old_url: Old site URL.
        new_url: New site URL.

    Returns:
        Similarity score 0-100.
    """
    try:
        old_path = urlparse(old_url).path.strip('/')
        new_path = urlparse(new_url).path.strip('/')

        if old_path == new_path:
            return 100

        ratio = SequenceMatcher(None, old_path, new_path).ratio()
        return int(ratio * 100)
    except Exception:
        return 0


def calculate_title_similarity(old_title: str, new_title: str) -> int:
    """
    Calculate similarity between page titles using SequenceMatcher.

    Args:
        old_title: Title from old page.
        new_title: Title from new page.

    Returns:
        Similarity score 0-100.
    """
    if not old_title or not new_title:
        return 0

    if old_title == new_title:
        return 100

    ratio = SequenceMatcher(None, old_title.lower(), new_title.lower()).ratio()
    return int(ratio * 100)


def transform_mapping_for_frontend(
    db_record: Dict[str, Any],
    title_map: Optional[Dict[str, str]] = None,
    gsc_metrics_map: Optional[Dict[str, Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Convert a single database mapping record to frontend format.

    Args:
        db_record: Database record from url_mappings table
        title_map: Optional URL-to-title lookup from webpage_embeddings
        gsc_metrics_map: Optional old-URL-to-metrics lookup from gsc_url_metrics

    Returns:
        Dictionary matching frontend RedirectMapping interface
    """
    # Convert confidence from 0.0-1.0 to 0-100
    confidence_score = db_record.get('confidence_score', 0.0)
    confidence_int = int(confidence_score * 100)

    # Calculate confidence band
    confidence_band = calculate_confidence_band(confidence_score)

    # Get warnings
    warnings = derive_warnings(db_record)

    # Calculate real similarity scores
    old_url = db_record['old_url']
    new_url = db_record['new_url']

    path_sim = calculate_path_similarity(old_url, new_url)
    content_sim = confidence_int  # confidence_score IS the content similarity

    title_sim = 0
    if title_map:
        old_title = title_map.get(old_url, '')
        new_title = title_map.get(new_url, '')
        title_sim = calculate_title_similarity(old_title, new_title)

    result = {
        'id': str(db_record['id']),
        'oldUrl': old_url,
        # Display stays a string; the precondition preserves null vs empty.
        'newUrl': new_url if new_url is not None else '',
        'persistedNewUrl': new_url,
        'confidence': confidence_int,
        'confidenceBand': confidence_band,
        'matchScore': confidence_int,
        'matchType': db_record.get('match_type', 'semantic'),
        'approved': not db_record.get('needs_review', False),
        'warnings': warnings,
        'pathSimilarity': path_sim,
        'titleSimilarity': title_sim,
        'contentSimilarity': content_sim
    }

    # A proposed better target, where the session's own rename conventions had
    # something to say about a flagged row. Advisory: newUrl above is still
    # what the matcher chose, and stays that way until someone accepts this.
    repaired_url = db_record.get('repaired_url')
    if repaired_url:
        result['repair'] = {
            'url': repaired_url,
            'method': db_record.get('repair_method'),
            'confidence': int(round((db_record.get('repair_confidence') or 0) * 100)),
            'support': db_record.get('repair_support'),
            'evidence': db_record.get('repair_evidence'),
        }

    if gsc_metrics_map is not None:
        metrics = gsc_metrics_map.get(old_url)
        result['gscClicks'] = int(metrics['clicks']) if metrics else 0
        result['gscImpressions'] = int(metrics['impressions']) if metrics else 0

    return result


def compute_risk_summary(
    mappings: List[Dict[str, Any]],
    traffic_share_target: float = 0.8,
) -> Dict[str, Any]:
    """
    Compute the traffic-risk headline numbers from mappings that already carry
    gscClicks/gscImpressions.

    Finds the smallest set of URLs carrying >= traffic_share_target of total
    clicks (falling back to impressions when a site has zero clicks) and counts
    how many of those lack a confident match (low band or needs-review).
    """
    total_clicks = sum(int(m.get('gscClicks') or 0) for m in mappings)
    total_impressions = sum(int(m.get('gscImpressions') or 0) for m in mappings)
    weight_key = 'gscClicks' if total_clicks > 0 else 'gscImpressions'
    total_weight = total_clicks if total_clicks > 0 else total_impressions

    summary = {
        'totalClicks': total_clicks,
        'totalImpressions': total_impressions,
        'weightMetric': 'clicks' if total_clicks > 0 else 'impressions',
        'trafficShareTarget': int(traffic_share_target * 100),
        'topUrlCount': 0,
        'topUrlsAtRisk': 0,
        'urlsWithTraffic': 0,
    }

    if total_weight <= 0:
        return summary

    weighted = sorted(
        (m for m in mappings if int(m.get(weight_key) or 0) > 0),
        key=lambda m: int(m.get(weight_key) or 0),
        reverse=True,
    )
    summary['urlsWithTraffic'] = len(weighted)

    running = 0
    top: List[Dict[str, Any]] = []
    for m in weighted:
        top.append(m)
        running += int(m.get(weight_key) or 0)
        if running / total_weight >= traffic_share_target:
            break

    at_risk = [
        m for m in top
        if m.get('confidenceBand') == 'low' or 'needs-review' in (m.get('warnings') or [])
    ]

    summary['topUrlCount'] = len(top)
    summary['topUrlsAtRisk'] = len(at_risk)
    return summary


def calculate_stats(mappings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calculate aggregate statistics from mappings.

    Args:
        mappings: List of frontend-formatted mapping dictionaries

    Returns:
        Dictionary with total, high, medium, low counts and approval progress
    """
    total = len(mappings)

    if total == 0:
        return {
            'total': 0,
            'high': 0,
            'medium': 0,
            'low': 0,
            'approved': 0,
            'approvalProgress': 0
        }

    high_count = len([m for m in mappings if m['confidenceBand'] == 'high'])
    medium_count = len([m for m in mappings if m['confidenceBand'] == 'medium'])
    low_count = len([m for m in mappings if m['confidenceBand'] == 'low'])
    approved_count = len([m for m in mappings if m['approved']])

    return {
        'total': total,
        'high': high_count,
        'medium': medium_count,
        'low': low_count,
        'approved': approved_count,
        'approvalProgress': round((approved_count / total) * 100)
    }


def _build_title_map(session_id: str) -> Dict[str, str]:
    """
    Build a URL-to-title lookup from webpage_embeddings for a session.

    Args:
        session_id: Migration session ID string.

    Returns:
        Dictionary mapping URL to page title.
    """
    try:
        from src.redirx.database import WebPageEmbeddingDB
        embedding_db = WebPageEmbeddingDB()
        embeddings = embedding_db.get_embeddings_by_session(
            UUID(session_id)
        )
        return {e['url']: e.get('title', '') for e in embeddings}
    except Exception:
        return {}


def format_results_response(
    db_mappings: List[Dict[str, Any]],
    session_metadata: Dict[str, Any] = None,
    gsc_metrics: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Format complete results response for frontend.

    Args:
        db_mappings: List of database mapping records
        session_metadata: Optional session information
        gsc_metrics: Optional gsc_url_metrics records for the session

    Returns:
        Complete response with mappings, stats, metadata, and (when Search
        Console data has been synced) per-URL traffic plus a risk summary
    """
    # Build title map from embeddings if we have a session
    # Skip for url_only pipelines (no embeddings exist)
    title_map = {}
    pipeline_type = session_metadata.get('pipeline_type', 'content') if session_metadata else 'content'
    if pipeline_type != 'url_only':
        if session_metadata and session_metadata.get('id'):
            title_map = _build_title_map(str(session_metadata['id']))
        elif db_mappings:
            session_id = db_mappings[0].get('session_id')
            if session_id:
                title_map = _build_title_map(str(session_id))

    # Search Console traffic data is only attached once a sync has happened
    gsc_synced = bool(session_metadata and session_metadata.get('gsc_synced_at'))
    gsc_metrics_map = None
    if gsc_synced:
        gsc_metrics_map = {m['url']: m for m in (gsc_metrics or [])}

    # Transform all mappings
    frontend_mappings = [
        transform_mapping_for_frontend(m, title_map, gsc_metrics_map)
        for m in db_mappings
    ]

    # Calculate stats
    stats = calculate_stats(frontend_mappings)

    response = {
        'success': True,
        'mappings': frontend_mappings,
        'stats': stats
    }

    if gsc_synced:
        response['gsc'] = {
            'synced': True,
            'property': session_metadata.get('gsc_property'),
            'synced_at': session_metadata.get('gsc_synced_at'),
            'riskSummary': compute_risk_summary(frontend_mappings),
        }
    else:
        response['gsc'] = {'synced': False}

    # Add session metadata if provided
    if session_metadata:
        response['session'] = {
            'id': str(session_metadata.get('id', '')),
            'status': session_metadata.get('status', 'unknown'),
            'created_at': session_metadata.get('created_at', ''),
            'user_id': session_metadata.get('user_id', ''),
            'pipeline_type': session_metadata.get('pipeline_type', 'content')
        }

    return response

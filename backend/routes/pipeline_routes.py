from flask import Blueprint, Response, request, jsonify
import os
import math
from uuid import UUID
import numpy as np
from backend.services.pipeline_runner import run_pipeline, read_csv
from backend.services.results_formatter import format_results_response, calculate_path_similarity, calculate_confidence_band
from backend.services.auth_service import require_auth
from backend.services.deep_preview_service import DeepPreviewService
from backend.services.pricing_service import PricingService
from backend.services import entitlement_service, redirect_export
from backend.services.inventory_policy import InventoryPolicyError, MAX_URL_LENGTH, normalize_origin
from backend.services.job_limits import (
    ContentJobUrlCapExceeded,
    validate_content_job_url_counts,
)
from backend.extensions import limiter
from src.redirx.config import Config
from src.redirx.database import (
    URLMappingDB,
    MigrationSessionDB,
    UserQuotaDB,
    WebPageEmbeddingDB,
    DeepMatchPreviewDB,
    GSCUrlMetricsDB,
)

pipeline_blueprint = Blueprint("pipeline", __name__)
MAX_UPLOAD_FILE_BYTES = int(os.getenv("MAX_UPLOAD_FILE_BYTES", str(10 * 1024 * 1024)))


@pipeline_blueprint.record_once
def _init_limiter(state):
    if "limiter" not in state.app.extensions:
        limiter.init_app(state.app)


def _get_upload_size(file_storage) -> int | None:
    """Compute file size without consuming the file stream."""
    if file_storage.content_length is not None and file_storage.content_length > 0:
        return int(file_storage.content_length)

    stream = getattr(file_storage, "stream", None)
    if not stream or not hasattr(stream, "seek") or not hasattr(stream, "tell"):
        return None

    try:
        current = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(current, os.SEEK_SET)
        return int(size)
    except Exception:
        return None


def _reject_if_too_large(file_storage, field_name: str):
    size = _get_upload_size(file_storage)
    if size is not None and size > MAX_UPLOAD_FILE_BYTES:
        return jsonify({
            "success": False,
            "error": f"{field_name} exceeds upload size limit",
            "max_file_bytes": MAX_UPLOAD_FILE_BYTES,
        }), 413
    return None


def _reject_if_content_url_cap_exceeded(
    old_urls: list[str],
    new_urls: list[str],
    pipeline_type: str,
):
    try:
        validate_content_job_url_counts(old_urls, new_urls, pipeline_type)
    except ContentJobUrlCapExceeded as e:
        return jsonify(e.to_api_payload()), 422
    return None


def _build_preview_copy(total_convincing_fixes: int, locked_count: int) -> dict[str, str]:
    return {
        'headline': 'Deep Match found redirect mistakes Quick Match missed.',
        'subheadline': (
            'We re-checked your riskiest URLs with content-level AI matching '
            f'and found {total_convincing_fixes} higher-confidence fixes.'
        ),
        'cta_primary': 'Unlock Every High-Confidence Fix',
        'cta_secondary': 'View Pricing',
        'lock_overlay': (
            f'Unlock {locked_count} more high-confidence fixes and AI alternatives before launch.'
        ),
    }


def _derive_locked_count(
    total_convincing_fixes: int,
    visible_count: int,
    locked_teaser_count: int,
) -> int:
    inferred_locked = max(0, total_convincing_fixes - max(0, visible_count))
    return max(max(0, locked_teaser_count), inferred_locked)


@pipeline_blueprint.route("/process", methods=["POST"])
@limiter.limit("20 per hour")
@require_auth
def process_csv():
    """
    Process old and new site CSV files through the Redirx pipeline.

    Expects:
        - old_csv: CSV file with old site URLs (first column)
        - new_csv: CSV file with new site URLs (first column)
        - Authorization header with Bearer token

    Returns:
        JSON response with session_id or error message
    """
    # Validate required files are present
    if "old_csv" not in request.files or "new_csv" not in request.files:
        return jsonify({
            "error": "Both 'old_csv' and 'new_csv' files are required"
        }), 400

    old_csv = request.files["old_csv"]
    new_csv = request.files["new_csv"]

    too_large = _reject_if_too_large(old_csv, "old_csv")
    if too_large:
        return too_large
    too_large = _reject_if_too_large(new_csv, "new_csv")
    if too_large:
        return too_large

    # Validate files are not empty
    if old_csv.filename == '':
        return jsonify({
            "error": "old_csv file is empty or not selected"
        }), 400

    if new_csv.filename == '':
        return jsonify({
            "error": "new_csv file is empty or not selected"
        }), 400

    # Validate file extensions (optional but recommended)
    allowed_extensions = {'.csv', '.txt'}
    old_ext = '.' + old_csv.filename.rsplit('.', 1)[-1].lower() if '.' in old_csv.filename else ''
    new_ext = '.' + new_csv.filename.rsplit('.', 1)[-1].lower() if '.' in new_csv.filename else ''

    if old_ext not in allowed_extensions:
        return jsonify({
            "error": f"old_csv must be a CSV file, got: {old_csv.filename}"
        }), 400

    if new_ext not in allowed_extensions:
        return jsonify({
            "error": f"new_csv must be a CSV file, got: {new_csv.filename}"
        }), 400

    # Get user_id from authenticated user
    user_id = str(request.user.id)

    # Get optional 'force' parameter from form data
    force = request.form.get('force', 'false').lower() == 'true'

    # Determine requested/default pipeline type. Deep Match is free at full
    # quality for every plan now (Pricing V3) — Quick Match remains available
    # as an explicit, faster opt-in, not the default a free plan is forced
    # into.
    requested_pipeline_type = request.form.get('pipeline_type')
    if requested_pipeline_type not in (None, 'content', 'url_only'):
        requested_pipeline_type = 'content'

    quota_db = UserQuotaDB()
    user_plan = quota_db.get_plan(user_id)
    pipeline_type = requested_pipeline_type or 'content'

    entitlement_decision = None
    if pipeline_type == 'content':
        entitlement_decision = entitlement_service.check_deep_match_run(user_id, user_plan)
        if not entitlement_decision.allowed:
            return jsonify({
                "success": False,
                "code": entitlement_decision.code,
                "error": entitlement_decision.user_message,
                "user_message": entitlement_decision.user_message,
                "next_action": entitlement_decision.next_action,
                "retryable": False,
            }), 429

    parsed_old_urls = None
    parsed_new_urls = None
    try:
        parsed_old_urls = read_csv(old_csv)
        parsed_new_urls = read_csv(new_csv)
    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    cap_error = _reject_if_content_url_cap_exceeded(
        parsed_old_urls,
        parsed_new_urls,
        pipeline_type,
    )
    if cap_error:
        return cap_error

    try:
        # Run the pipeline
        session_id, is_duplicate = run_pipeline(
            old_csv, new_csv,
            user_id=user_id,
            force=force,
            pipeline_type=pipeline_type,
            old_urls=parsed_old_urls,
            new_urls=parsed_new_urls,
            priority=(entitlement_decision.priority if entitlement_decision else 0),
        )

        # Only a genuinely new job drew on the worker; a duplicate returned
        # by idempotency didn't, and must not consume the ceiling twice.
        if pipeline_type == 'content' and not is_duplicate:
            entitlement_service.record_deep_match_run(user_id, session_id, user_plan)

        response = {
            "success": True,
            "message": "Pipeline completed successfully",
            "session_id": str(session_id),
            "is_duplicate": is_duplicate,
            "pipeline_type": pipeline_type
        }
        if entitlement_decision and entitlement_decision.warning:
            response["warning"] = entitlement_decision.warning
            response["warning_message"] = entitlement_decision.warning_message
        return jsonify(response), 200

    except ContentJobUrlCapExceeded as e:
        return jsonify(e.to_api_payload()), 422

    except ValueError as e:
        # CSV parsing or validation errors
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    except RuntimeError as e:
        # Pipeline execution errors
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    except Exception as e:
        # Unexpected errors
        return jsonify({
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }), 500


@pipeline_blueprint.route("/results/<session_id>", methods=["GET"])
@require_auth
def get_results(session_id: str):
    """
    Retrieve pipeline results for a given session.

    Args:
        session_id: UUID string of the migration session

    Returns:
        JSON response with:
            - mappings: List of redirect mappings
            - stats: Aggregate statistics (total, confidence bands, approval)
            - session: Session metadata
    """
    print(f"DEBUG: get_results called with session_id={session_id}")
    try:
        # Validate session_id is a valid UUID
        try:
            session_uuid = UUID(session_id)
        except ValueError:
            return jsonify({
                "success": False,
                "error": f"Invalid session_id format: {session_id}"
            }), 400

        # Get session metadata
        session_db = MigrationSessionDB()
        try:
            session_metadata = session_db.get_session(session_uuid)
        except ValueError as e:
            return jsonify({
                "success": False,
                "error": str(e)
            }), 404

        # Verify session belongs to authenticated user
        if session_metadata.get('user_id') != str(request.user.id):
            return jsonify({
                "success": False,
                "error": "Unauthorized: Session belongs to another user"
            }), 403

        # Get mappings for this session
        mapping_db = URLMappingDB()
        db_mappings = mapping_db.get_mappings_by_session(session_uuid)

        # Attach Search Console traffic metrics if this session was synced
        gsc_metrics = None
        if session_metadata.get('gsc_synced_at'):
            try:
                gsc_metrics = GSCUrlMetricsDB().get_metrics_by_session(session_uuid)
            except Exception:
                gsc_metrics = None

        # Transform data for frontend
        response = format_results_response(db_mappings, session_metadata, gsc_metrics)

        return jsonify(response), 200

    except Exception as e:
        # Unexpected errors
        return jsonify({
            "success": False,
            "error": f"Failed to retrieve results: {str(e)}"
        }), 500


@pipeline_blueprint.route("/results/<session_id>/export", methods=["GET"])
@require_auth
def export_results(session_id: str):
    """
    A deploy-ready redirect file for a browser session.

    Viewing results is free; this is the Pricing V3 gate. `?format=` one of
    apache|nginx|wordpress|vercel|cloudflare|shopify|csv|json, `?url_format=`
    paths|full|custom (paths by default). Optional confidence_levels selects
    display bands; preview=true returns the same rendered bytes and exclusions
    without recording an export. Ownership and entitlement gate both modes.
    Mirrors v1_routes.export_migration through the same redirect_export module.
    """
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return jsonify({
            "success": False,
            "error": f"Invalid session_id format: {session_id}"
        }), 400

    user_id = str(request.user.id)
    session_db = MigrationSessionDB()
    try:
        session_metadata = session_db.get_session(session_uuid)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    if session_metadata.get('user_id') != user_id:
        return jsonify({
            "success": False,
            "error": "Unauthorized: Session belongs to another user"
        }), 403

    user_plan = UserQuotaDB().get_plan(user_id)
    decision = entitlement_service.check_export(user_id, user_plan, session_uuid)
    if not decision.allowed:
        return jsonify({
            "success": False,
            "code": decision.code,
            "error": decision.user_message,
            "user_message": decision.user_message,
            "next_action": decision.next_action,
            "retryable": False,
            **decision.extra,
        }), 402

    fmt = (request.args.get("format") or "csv").lower()
    if fmt not in redirect_export.FORMATS:
        return jsonify({
            "success": False,
            "error": f"'format' must be one of: {', '.join(redirect_export.FORMATS)}.",
        }), 400

    url_format = (request.args.get("url_format") or "paths").lower()
    if url_format not in ("paths", "full", "custom"):
        return jsonify({"success": False, "error": "'url_format' must be 'paths', 'full', or 'custom'."}), 400

    domains = {}
    for name in ("old_domain", "new_domain"):
        value = request.args.get(name) or None
        if value:
            try:
                # Validate against the shared origin policy, but keep the supplied
                # spelling for export. URL suffixes remain the renderer's raw input.
                normalize_origin(value)
            except InventoryPolicyError:
                return jsonify({"success": False, "error": f"'{name}' must be an http(s) origin without credentials, path, query or fragment."}), 400
        domains[name] = value if url_format == "custom" else None
    if url_format == "custom" and not any(domains.values()):
        return jsonify({"success": False, "error": "Custom domains require an old or new origin."}), 400

    try:
        min_confidence = float(request.args.get("min_confidence", 0))
        if not math.isfinite(min_confidence):
            raise ValueError()
    except ValueError:
        return jsonify({"success": False, "error": "'min_confidence' must be a finite number."}), 400
    levels = None
    if "confidence_levels" in request.args:
        levels = set(request.args["confidence_levels"].split(","))
        if not levels or not levels.issubset({"high", "medium", "low"}):
            return jsonify({"success": False, "error": "'confidence_levels' must contain high, medium or low, separated by commas."}), 400

    rows = [
        row for row in URLMappingDB().get_mappings_by_session(session_uuid)
        if (row.get("confidence_score") or 0) >= min_confidence
        and (levels is None or calculate_confidence_band(row.get("confidence_score") or 0) in levels)
    ]
    try:
        selection = redirect_export.select_export_mappings(rows, url_format=url_format, **domains)
        content = redirect_export.build_export(rows, fmt, url_format=url_format, **domains)
    except redirect_export.ExportSelectionError as error:
        return jsonify({"success": False, "code": "unsupported_export_input", "error": str(error),
                        "user_message": str(error), "retryable": False,
                        "next_action": "review_export_options"}), 422
    warning = redirect_export.warning_for(fmt, url_format)
    if request.args.get("preview", "").lower() == "true":
        response = jsonify({
            "success": True, "content": content,
            "filename": redirect_export.filename_for(fmt),
            "content_type": redirect_export.content_type_for(fmt),
            "included_count": selection["included_count"],
            "excluded_count": selection["excluded_count"],
            "excluded": [{"mapping_id": str(rows[item["index"]].get("id")) if rows[item["index"]].get("id") else None,
                          "reason": item["reason"]} for item in selection["excluded"]],
            "warning": warning,
        })
        response.headers["Cache-Control"] = "private, no-store"
        return response

    entitlement_service.record_export(user_id, session_uuid)
    response = Response(content, mimetype=redirect_export.content_type_for(fmt))
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{redirect_export.filename_for(fmt)}"'
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Redirect-Count"] = str(selection["included_count"])
    response.headers["X-Redirect-Excluded-Count"] = str(selection["excluded_count"])
    if warning:
        response.headers["X-Redirx-Warning"] = warning
    return response


@pipeline_blueprint.route("/results/<session_id>/deep-preview", methods=["GET"])
@require_auth
def get_deep_preview(session_id: str):
    """
    Retrieve Deep Match conversion preview snapshot for a source url_only session.

    Status values:
      - queued | processing | completed | failed | skipped | not_applicable
    """
    try:
        try:
            session_uuid = UUID(session_id)
        except ValueError:
            return jsonify({
                "success": False,
                "error": f"Invalid session_id format: {session_id}"
            }), 400

        session_db = MigrationSessionDB()
        try:
            session = session_db.get_session(session_uuid)
        except ValueError as e:
            return jsonify({
                "success": False,
                "error": str(e)
            }), 404

        user_id = str(request.user.id)
        if session.get('user_id') != user_id:
            return jsonify({
                "success": False,
                "error": "Unauthorized: Session belongs to another user"
            }), 403

        def _not_applicable(reason: str):
            return jsonify({
                "success": True,
                "status": "not_applicable",
                "reason": reason,
                "source_session_id": str(session_uuid),
                "free_unlock_count": max(1, Config.PREVIEW_FREE_ROWS),
                "total_convincing_fixes": 0,
                "visible_items": [],
                "locked_teasers": [],
                **_build_preview_copy(total_convincing_fixes=0, locked_count=0),
            }), 200

        if not Config.ENABLE_DEEP_MATCH_PREVIEW:
            return _not_applicable("feature_disabled")

        if session.get('is_preview'):
            return _not_applicable("preview_session_not_supported")

        if session.get('pipeline_type') != 'url_only':
            return _not_applicable("source_pipeline_not_url_only")

        quota_db = UserQuotaDB()
        if quota_db.get_plan(user_id) != 'free':
            return _not_applicable("plan_not_free")

        preview_db = DeepMatchPreviewDB()
        preview = preview_db.get_by_source_session(session_uuid)
        if not preview:
            source_status = (session.get('status') or '').lower()
            if source_status in ('completed', 'permanently_failed'):
                # Backfill for idempotent/older sessions: try to queue preview on-demand.
                try:
                    preview_service = DeepPreviewService()
                    kickoff = preview_service.maybe_queue_preview(
                        source_session_id=session_uuid,
                        user_id=user_id,
                        old_urls=session.get('old_urls') or [],
                        new_urls=session.get('new_urls') or [],
                    )
                except Exception as kickoff_error:
                    return jsonify({
                        "success": True,
                        "status": "failed",
                        "reason": "preview_backfill_failed",
                        "source_session_id": str(session_uuid),
                        "free_unlock_count": max(1, Config.PREVIEW_FREE_ROWS),
                        "total_convincing_fixes": 0,
                        "visible_items": [],
                        "locked_teasers": [],
                        "error_message": str(kickoff_error),
                        **_build_preview_copy(total_convincing_fixes=0, locked_count=0),
                    }), 200

                preview = preview_db.get_by_source_session(session_uuid)
                if not preview and kickoff.get('status') in ('skipped', 'not_applicable'):
                    return jsonify({
                        "success": True,
                        "status": kickoff.get('status'),
                        "reason": kickoff.get('reason'),
                        "source_session_id": str(session_uuid),
                        "free_unlock_count": max(1, Config.PREVIEW_FREE_ROWS),
                        "total_convincing_fixes": 0,
                        "visible_items": [],
                        "locked_teasers": [],
                        **_build_preview_copy(total_convincing_fixes=0, locked_count=0),
                    }), 200

                if not preview and kickoff.get('status') == 'not_applicable':
                    return _not_applicable(kickoff.get('reason') or "not_applicable")

            if not preview:
                return jsonify({
                    "success": True,
                    "status": "queued",
                    "source_session_id": str(session_uuid),
                    "free_unlock_count": max(1, Config.PREVIEW_FREE_ROWS),
                    "total_convincing_fixes": 0,
                    "visible_items": [],
                    "locked_teasers": [],
                    **_build_preview_copy(total_convincing_fixes=0, locked_count=0),
                }), 200

        # Self-heal stale preview rows: if preview session already finished but
        # snapshot status is still queued/processing, finalize from source data.
        preview_status = (preview.get('status') or '').lower()
        preview_session_id_raw = preview.get('preview_session_id')
        if preview_status in ('queued', 'processing') and preview_session_id_raw:
            try:
                preview_session_uuid = UUID(str(preview_session_id_raw))
                preview_session = session_db.get_session(preview_session_uuid)
                preview_session_status = (preview_session.get('status') or '').lower()

                if preview_session_status == 'completed':
                    DeepPreviewService().finalize_preview(preview_session_uuid)
                    refreshed = preview_db.get_by_source_session(session_uuid)
                    if refreshed:
                        preview = refreshed
                elif preview_session_status == 'permanently_failed':
                    preview_db.mark_failed_by_preview_session(
                        preview_session_uuid,
                        'Preview run failed before completion.',
                    )
                    refreshed = preview_db.get_by_source_session(session_uuid)
                    if refreshed:
                        preview = refreshed
                elif preview_session_status == 'processing' and preview_status == 'queued':
                    preview_db.mark_processing_by_preview_session(preview_session_uuid)
                    refreshed = preview_db.get_by_source_session(session_uuid)
                    if refreshed:
                        preview = refreshed
            except Exception:
                # Non-blocking: if healing fails we still return current snapshot state.
                pass

        status = preview.get('status', 'queued')
        total = int(preview.get('total_convincing_fixes') or 0)
        visible_items = preview.get('visible_items') or []
        locked_teasers = preview.get('locked_teasers') or []
        free_unlock_count = int(preview.get('free_unlock_count') or max(1, Config.PREVIEW_FREE_ROWS))
        locked_count = _derive_locked_count(
            total_convincing_fixes=total,
            visible_count=len(visible_items),
            locked_teaser_count=len(locked_teasers),
        )

        payload = {
            "success": True,
            "status": status,
            "source_session_id": str(session_uuid),
            "free_unlock_count": free_unlock_count,
            "total_convincing_fixes": total,
            "visible_items": visible_items,
            "locked_teasers": locked_teasers,
            "error_message": preview.get('error_message'),
            **_build_preview_copy(total_convincing_fixes=total, locked_count=locked_count),
        }
        return jsonify(payload), 200

    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to retrieve deep preview: {str(e)}"
        }), 500


@pipeline_blueprint.route("/projects/<source_session_id>/unlock-status", methods=["GET"])
@require_auth
def get_project_unlock_status(source_session_id: str):
    """
    Get unlock/payment/deep-run status for a Quick Match source session.
    """
    try:
        try:
            source_uuid = UUID(source_session_id)
        except ValueError:
            return jsonify({
                "success": False,
                "error": f"Invalid source_session_id format: {source_session_id}",
            }), 400

        status = PricingService().get_unlock_status(
            source_session_id=source_uuid,
            user_id=str(request.user.id),
        )
        return jsonify({"success": True, **status}), 200
    except ValueError as e:
        message = str(e).lower()
        if "does not belong" in message:
            return jsonify({
                "success": False,
                "error": "Unauthorized: Session belongs to another user",
            }), 403
        return jsonify({
            "success": False,
            "error": str(e),
        }), 404
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to load unlock status: {str(e)}",
        }), 500


@pipeline_blueprint.route("/results/<session_id>/alternatives/<mapping_id>", methods=["GET"])
@require_auth
def get_alternatives(session_id: str, mapping_id: str):
    """
    Retrieve alternative candidate URLs for a given mapping.

    Uses the old URL's stored embedding to find similar pages in the new site
    via vector similarity search, excluding the current match.

    Args:
        session_id: UUID string of the migration session
        mapping_id: UUID string of the URL mapping

    Returns:
        JSON response with list of alternative candidates
    """
    try:
        # Validate UUIDs
        try:
            session_uuid = UUID(session_id)
            mapping_uuid = UUID(mapping_id)
        except ValueError:
            return jsonify({
                "success": False,
                "error": "Invalid session_id or mapping_id format"
            }), 400

        # Fetch the mapping
        mapping_db = URLMappingDB()
        mapping = mapping_db.get_mapping_by_id(mapping_uuid)

        if not mapping:
            return jsonify({
                "success": False,
                "error": "Mapping not found"
            }), 404

        # Verify mapping belongs to the session
        if mapping['session_id'] != str(session_uuid):
            return jsonify({
                "success": False,
                "error": "Mapping does not belong to this session"
            }), 403

        # Check if this is a url_only session (no embeddings exist)
        session_db = MigrationSessionDB()
        try:
            session = session_db.get_session(session_uuid)
        except ValueError:
            return jsonify({
                "success": False,
                "error": "Session not found"
            }), 404

        # Verify session belongs to authenticated user
        if session.get('user_id') != str(request.user.id):
            return jsonify({
                "success": False,
                "error": "Unauthorized: Session belongs to another user"
            }), 403

        if session.get('pipeline_type') == 'url_only':
            return jsonify({
                "success": True,
                "alternatives": [],
                "message": (
                    "Alternatives are only available after Deep Match is unlocked "
                    "for this project or with an Agency subscription."
                ),
            }), 200

        old_url = mapping['old_url']
        current_new_url = mapping['new_url']

        # Look up the old URL's embedding
        embedding_db = WebPageEmbeddingDB()
        embeddings = embedding_db.get_embeddings_by_session(session_uuid, site_type='old')

        old_embedding = None
        for emb in embeddings:
            if emb['url'] == old_url:
                old_embedding = emb
                break

        if not old_embedding:
            return jsonify({
                "success": True,
                "alternatives": [],
                "message": "No embedding found for this URL. Exact URL matches skip the embedding stage."
            }), 200

        # Convert embedding to numpy array for the RPC call
        query_vector = np.array(old_embedding['embedding'], dtype=np.float32)

        # Find similar pages in the new site (request 6 to allow filtering out current match)
        candidates = embedding_db.find_similar_pages(
            query_embedding=query_vector,
            session_id=session_uuid,
            site_type='new',
            match_count=6
        )

        # Build alternatives list, excluding the current new_url
        alternatives = []
        for candidate in candidates:
            if candidate['url'] == current_new_url:
                continue

            similarity = int(candidate.get('similarity', 0) * 100)
            path_sim = calculate_path_similarity(old_url, candidate['url'])

            alternatives.append({
                "url": candidate['url'],
                "similarity": similarity,
                "title": candidate.get('title', ''),
                "pathSimilarity": path_sim
            })

        return jsonify({
            "success": True,
            "alternatives": alternatives
        }), 200

    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to retrieve alternatives: {str(e)}"
        }), 500


@pipeline_blueprint.route("/results/<session_id>/mappings/<mapping_id>", methods=["PATCH"])
@require_auth
def update_mapping(session_id: str, mapping_id: str):
    """
    Persist a review decision.

    Until now approve, inline-edit and accept-repair lived only in React state,
    which made the database's needs_review a fiction everything downstream
    believed: the watch monitored only pipeline-confident rows and compared
    live sites against pre-edit targets (filing wrong_target for correctly
    deployed fixes), and the v1 export could disagree with the file the browser
    built. Review decisions are product data, not UI state.

    Accepts a partial body. Target/approval writes also require expected_new_url:
      approved     bool   -> stored as needs_review = not approved
      new_url      str    -> the reviewed target (edit or accepted repair)
      match_type   str    -> e.g. 'manual', 'rule_repaired'
      confidence   float  -> 0..1
      clear_repair bool   -> drop any pending repair proposal
      expected_new_url str | null -> exact saved target before any decision/target write
    """
    try:
        session_uuid = UUID(session_id)
        mapping_uuid = UUID(mapping_id)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid id format."}), 400

    session_db = MigrationSessionDB()
    try:
        session_metadata = session_db.get_session(session_uuid)
    except ValueError:
        return jsonify({"success": False, "error": "Session not found."}), 404

    if session_metadata.get('user_id') != str(request.user.id):
        return jsonify({
            "success": False,
            "error": "Unauthorized: Session belongs to another user"
        }), 403

    mapping_db = URLMappingDB()
    mapping = mapping_db.get_mapping_by_id(mapping_uuid)
    # Session-scoped: a mapping id from someone else's session must 404 here
    # even though the session check above passed for *this* session.
    if not mapping or str(mapping.get('session_id')) != str(session_uuid):
        return jsonify({"success": False, "error": "Mapping not found."}), 404

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'success': False, 'error': 'Review decision must be a JSON object.'}), 400
    target_bound = 'approved' in body or 'new_url' in body
    if target_bound and 'expected_new_url' not in body:
        return jsonify({
            "success": False, "code": "review_target_precondition_required",
            "error": "Reload the review page before saving. The exact previously reviewed target is required.",
            "retryable": False, "next_action": "reload_results",
        }), 409
    expected_target = body.get('expected_new_url')
    if target_bound:
        if expected_target is not None and (not isinstance(expected_target, str) or len(expected_target) > MAX_URL_LENGTH):
            return jsonify({"success": False, "error": "'expected_new_url' must be a URL string or null."}), 400
        if expected_target != mapping.get('new_url'):
            return jsonify({
                "success": False, "code": "review_target_conflict",
                "error": "The saved target changed. Reload results and review the current target before saving.",
                "retryable": False, "next_action": "reload_results",
            }), 409
    updates = {}

    if 'approved' in body:
        if not isinstance(body['approved'], bool):
            return jsonify({"success": False, "error": "'approved' must be a boolean."}), 400
        updates['needs_review'] = not body['approved']

    if 'new_url' in body:
        new_url = body['new_url']
        if not isinstance(new_url, str) or not new_url.strip() or len(new_url) > 2048:
            return jsonify({"success": False, "error": "'new_url' must be a non-empty URL."}), 400
        updates['new_url'] = new_url.strip()
        # Approval is about this target, not merely the source row. Historical
        # callers may omit approved; changing a target then requires review.
        if updates['new_url'] != mapping.get('new_url') and 'approved' not in body:
            updates['needs_review'] = True

    if 'match_type' in body:
        match_type = body['match_type']
        if not isinstance(match_type, str) or not (0 < len(match_type) <= 40):
            return jsonify({"success": False, "error": "'match_type' must be a short string."}), 400
        updates['match_type'] = match_type

    if 'confidence' in body:
        try:
            confidence = float(body['confidence'])
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "'confidence' must be a number."}), 400
        if not 0.0 <= confidence <= 1.0:
            return jsonify({"success": False, "error": "'confidence' must be between 0 and 1."}), 400
        updates['confidence_score'] = confidence

    if body.get('clear_repair'):
        updates.update({
            'repaired_url': None,
            'repair_method': None,
            'repair_confidence': None,
            'repair_support': None,
            'repair_evidence': None,
        })

    if not updates:
        return jsonify({"success": False, "error": "Nothing to update."}), 400

    def unconfirmed(status):
        return jsonify({
            "success": False,
            "code": "review_save_unconfirmed",
            "error": "Review save was not confirmed. Reload results to check the server state before exporting.",
            "retryable": False,
            "next_action": "reload_results",
        }), status

    try:
        # url_mappings has no owner column. Bind the mutation to the session
        # whose owner was checked above, as well as the mapping ID.
        mutation = mapping_db.client.table('url_mappings').update(updates).eq(
            'id', str(mapping_uuid)
        ).eq('session_id', str(session_uuid))
        if target_bound:
            # Atomic comparison: another tab can change the target after our
            # read. Null and the empty string remain distinct stored identities.
            mutation = (mutation.is_('new_url', 'null') if expected_target is None
                        else mutation.eq('new_url', expected_target))
        result = mutation.execute()
    except Exception:
        # The write might have committed before the response failed. Never
        # imply rollback or expose database detail; require readback.
        return unconfirmed(503)
    if getattr(result, 'error', None):
        return unconfirmed(503)
    rows = getattr(result, 'data', None)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return unconfirmed(409)
    saved = rows[0]
    if (str(saved.get('id')) != str(mapping_uuid)
            or str(saved.get('session_id')) != str(session_uuid)
            or (target_bound and ('new_url' not in saved or saved['new_url'] != updates.get('new_url', expected_target)))
            or any(key not in saved or saved[key] != value
                   or (isinstance(value, bool) and not isinstance(saved[key], bool))
                   for key, value in updates.items())):
        return unconfirmed(409)

    return jsonify({"success": True}), 200

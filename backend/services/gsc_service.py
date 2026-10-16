"""
Google Search Console integration.

Handles a standalone Google OAuth flow (separate from Supabase login) scoped to
the Search Console read-only API, token storage/refresh, and pulling per-URL
clicks/impressions from the Search Analytics API so redirect mappings can be
triaged by real organic traffic instead of alphabetical order.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse
import logging
import hashlib
from uuid import UUID, uuid4

import jwt
import requests

from src.redirx.config import Config
from src.redirx.database import GSCConnectionDB, GSCUrlMetricsDB

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GSC_SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"
GSC_QUERY_URL = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"

GSC_SCOPES = "openid email https://www.googleapis.com/auth/webmasters.readonly"
STATE_TTL_SECONDS = 600
SEARCH_ANALYTICS_ROW_LIMIT = 25000
# GSC search analytics data lags ~2 days behind real time.
GSC_DATA_LAG_DAYS = 2

HTTP_TIMEOUT = 30


class GSCError(Exception):
    """User-facing Search Console integration error."""

    def __init__(self, code: str, user_message: str, status_code: int = 400):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.status_code = status_code


# ============================================================================
# Pure helpers (unit-testable without network or DB)
# ============================================================================

def normalize_url(url: str) -> str:
    """
    Identity key for "is this the same page", used to match session URLs
    against GSC page URLs and to union discovery sources.

    Scheme and a leading www. are deliberately excluded. A sitemap may list
    http:// or the bare host while Search Console reports https://www., and
    those are the same page — keying on them produced duplicate URLs and
    split one page's traffic across two entries.
    """
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or '').lower()
        if host.startswith('www.'):
            host = host[4:]
        port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ''
        path = parsed.path.rstrip('/') or ''
        return f"{host}{port}{path}"
    except Exception:
        return url.strip().rstrip('/').lower()


def _registrable_hosts(urls: List[str]) -> List[str]:
    hosts = []
    for url in urls:
        try:
            host = urlparse(url).netloc.lower().split(':')[0]
            if host:
                hosts.append(host)
        except Exception:
            continue
    return hosts


def _strip_www(host: str) -> str:
    host = (host or '').lower().split(':')[0]
    return host[4:] if host.startswith('www.') else host


def property_matches_host(site_url: str, host: str) -> bool:
    """
    Whether a GSC property covers a hostname.

    Domain properties ("sc-domain:example.com") cover all subdomains.
    URL-prefix properties ("https://www.example.com/") are compared
    www-insensitively: Google treats www and apex as separate properties, but
    a user pasting "example.com" whose only verified property is the www form
    plainly means that property. Requiring an exact match meant GSC discovery
    silently never activated for such sites — the common case — and quietly
    degraded to sitemap-only forever.
    """
    host = _strip_www(host)
    if site_url.startswith('sc-domain:'):
        domain = site_url[len('sc-domain:'):].lower()
        return host == domain or host.endswith('.' + domain)
    prop_host = _strip_www(urlparse(site_url).netloc)
    return bool(prop_host) and prop_host == host


def pick_property_for_urls(properties: List[str], urls: List[str]) -> Optional[str]:
    """
    Pick the verified GSC property that covers the most of the given URLs.
    Returns None if no property covers any of them.
    """
    hosts = _registrable_hosts(urls)
    if not hosts or not properties:
        return None

    best: Tuple[int, Optional[str]] = (0, None)
    for site_url in properties:
        covered = sum(1 for host in hosts if property_matches_host(site_url, host))
        if covered > best[0]:
            best = (covered, site_url)
    return best[1]


# ============================================================================
# OAuth state tokens
# ============================================================================

def create_state_token(user_id: str, return_to: str = '/') -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            'sub': user_id,
            'return_to': return_to,
            'purpose': 'gsc_oauth',
            'jti': uuid4().hex,
            'iat': now,
            'exp': now + timedelta(seconds=STATE_TTL_SECONDS),
        },
        Config.GSC_STATE_SECRET,
        algorithm='HS256',
    )


def verify_state_token(state: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(state, Config.GSC_STATE_SECRET, algorithms=['HS256'])
    except jwt.PyJWTError as exc:
        raise GSCError(
            code='gsc_invalid_state',
            user_message='The Search Console connection link expired. Please try again.',
            status_code=400,
        ) from exc

    if payload.get('purpose') != 'gsc_oauth' or not payload.get('sub'):
        raise GSCError(
            code='gsc_invalid_state',
            user_message='The Search Console connection link is invalid. Please try again.',
            status_code=400,
        )
    return payload


# ============================================================================
# Service
# ============================================================================

class GSCService:
    """OAuth + Search Analytics operations against the Google APIs."""

    def __init__(
        self,
        connection_db: Optional[GSCConnectionDB] = None,
        metrics_db: Optional[GSCUrlMetricsDB] = None,
    ):
        Config.validate_gsc()
        self.connection_db = connection_db or GSCConnectionDB()
        self.metrics_db = metrics_db or GSCUrlMetricsDB()

    # --- OAuth flow ---

    def _consent_rpc(self, name: str, params: Dict[str, Any]):
        try:
            return self.connection_db.client.rpc(name, params).execute().data
        except Exception as exc:
            if getattr(exc, 'code', None) == 'P0001' and getattr(exc, 'message', None) in {
                'invalid_input', 'operation_conflict', 'not_found',
            }:
                raise GSCError('gsc_invalid_state',
                    'This Search Console consent is expired, already used, or superseded. Start a new connection.',400) from None
            raise GSCError('gsc_connection_unconfirmed',
                'The connection attempt could not be confirmed. Reload connection status before starting fresh consent.',503) from None

    @staticmethod
    def _check_consent_operation(op, payload, state_hash, status):
        try:
            valid = (isinstance(op, dict) and op.get('user_id') == payload['sub']
                and op.get('action') == 'connect' and op.get('status') == status
                and op.get('state_hash') == state_hash
                and op.get('idempotency_key') == 'legacy:' + payload['jti']
                and op.get('request') == {'protocol': 'legacy_gsc_oauth', 'return_to': payload['return_to']}
                and type(op.get('epoch')) is int and op['epoch'] >= 1
                and str(UUID(op['id'])) == op['id'])
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise GSCError('gsc_connection_unconfirmed',
                'The connection attempt could not be confirmed. Reload connection status before starting fresh consent.',503)
        return op

    def build_authorization_url(self, user_id: str, return_to: str = '/') -> str:
        state = create_state_token(user_id, return_to)
        payload = verify_state_token(state)
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        op = self._consent_rpc('reserve_gsc_agent_operation', {
            'p_user_id': user_id, 'p_action': 'connect', 'p_key': 'legacy:' + payload['jti'],
            'p_request': {'protocol': 'legacy_gsc_oauth', 'return_to': return_to},
            'p_state_hash': state_hash,
        })
        self._check_consent_operation(op, payload, state_hash, 'awaiting_consent')
        params = {
            'client_id': Config.GOOGLE_OAUTH_CLIENT_ID,
            'redirect_uri': Config.GSC_OAUTH_REDIRECT_URI,
            'response_type': 'code',
            'scope': GSC_SCOPES,
            'access_type': 'offline',
            # Force consent so Google always returns a refresh_token.
            'prompt': 'consent',
            'state': state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def handle_callback(self, code: Optional[str], state: str, error: Optional[str] = None) -> Dict[str, Any]:
        """Consume historical consent once and publish only at its current epoch."""
        if not isinstance(state, str) or len(state) > 8192:
            raise GSCError('gsc_invalid_state','Invalid Search Console consent. Start a new connection.',400)
        payload = verify_state_token(state)
        # Old stateless links cannot prove current authority; require fresh consent.
        try:
            if (not isinstance(payload.get('jti'), str) or len(payload['jti']) != 32
                    or UUID(payload['jti']).hex != payload['jti']
                    or str(UUID(payload['sub'])) != payload['sub']
                    or not isinstance(payload.get('return_to'), str)):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise GSCError('gsc_invalid_state','This consent link needs to be renewed. Start a new connection.',400) from None
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        op = self._consent_rpc('consume_gsc_agent_state', {'p_hash': state_hash})
        self._check_consent_operation(op, payload, state_hash, 'running')
        user_id = payload['sub']

        def finish(status, tokens=None):
            result = {'status': status, 'user_id': user_id, 'operation_id': op['id']}
            receipt = self._consent_rpc('finish_gsc_agent_operation', {
                'p_user_id': user_id, 'p_id': op['id'], 'p_result': result,
                # Failed/denied consent preserves credentials; shared state stays connecting.
                'p_state': 'connected' if tokens is not None else None, 'p_tokens': tokens,
            })
            if receipt != result:
                raise GSCError('gsc_connection_unconfirmed',
                    'The connection attempt could not be confirmed. Reload connection status before starting fresh consent.',503)

        if error or not isinstance(code, str) or not code or len(code) > 4096:
            finish('cancelled')
            return {**payload, 'consent_result': 'cancelled'}
        try:
            response = requests.post(
                GOOGLE_TOKEN_URL,
                data={'code': code, 'client_id': Config.GOOGLE_OAUTH_CLIENT_ID,
                    'client_secret': Config.GOOGLE_OAUTH_CLIENT_SECRET,
                    'redirect_uri': Config.GSC_OAUTH_REDIRECT_URI, 'grant_type': 'authorization_code'},
                timeout=HTTP_TIMEOUT,
            )
            if not response.ok:
                raise GSCError('gsc_token_exchange_failed',
                    'Google rejected the connection attempt. Start a new connection.',502)
            tokens = response.json()
            if not isinstance(tokens, dict) or any(not isinstance(tokens.get(key), str) or not tokens[key]
                    for key in ('access_token', 'refresh_token')):
                raise GSCError('gsc_missing_tokens',
                    'Google did not grant offline access. Reconnect and approve all requested permissions.',502)
            # OAuth permits omission only when the granted scope equals the request.
            # An explicit partial/empty grant must never fall back to requested access.
            granted_scope = tokens.get('scope', GSC_SCOPES)
            if (not isinstance(granted_scope, str)
                    or 'https://www.googleapis.com/auth/webmasters.readonly' not in granted_scope.split()):
                raise GSCError('gsc_missing_scope',
                    'Search Console read access was not granted. Reconnect when you want to grant it.',409)
            google_email = None
            if tokens.get('id_token'):
                try:
                    # Returned directly by the configured Google TLS token endpoint.
                    google_email = jwt.decode(tokens['id_token'], options={'verify_signature': False}).get('email')
                except Exception:
                    pass
            published_tokens = {'access_token': tokens['access_token'], 'refresh_token': tokens['refresh_token'],
                'token_expires_at': self._expiry_iso(tokens.get('expires_in')),
                'google_email': google_email, 'scopes': granted_scope}
        except (requests.RequestException, ValueError, TypeError, OverflowError, GSCError) as exc:
            finish('failed')
            if isinstance(exc, GSCError):
                raise exc from None
            raise GSCError('gsc_token_exchange_failed',
                'Google could not complete this connection. Reload status before starting fresh consent.',502) from None
        # No retry if publication committed but its acknowledgment was lost.
        finish('succeeded', published_tokens)
        return {**payload, 'consent_result': 'connected'}

    def disconnect(self, user_id: str) -> None:
        try:
            result = self.connection_db.client.rpc('disconnect_legacy_gsc_connection', {
                'p_user_id': user_id,
            }).execute()
            receipt = getattr(result, 'data', None)
        except Exception:
            raise GSCError('gsc_disconnect_unconfirmed',
                'Disconnection could not be confirmed. Reload connection status before trying again.',503) from None
        if (not isinstance(receipt, dict) or receipt.get('user_id') != str(user_id)
                or receipt.get('disconnected') is not True
                or type(receipt.get('epoch')) is not int or receipt['epoch'] < 1
                or type(receipt.get('deleted_count')) is not int or receipt['deleted_count'] not in (0,1)
                or (receipt['deleted_count'] == 0 and receipt.get('revocation_token') is not None)
                or (receipt['deleted_count'] == 1 and (not isinstance(receipt.get('revocation_token'),str)
                    or not receipt['revocation_token']))):
            raise GSCError('gsc_disconnect_unconfirmed',
                'Disconnection could not be confirmed. Reload connection status before trying again.',503)
        if not receipt['deleted_count']:
            return
        try:
            requests.post(
                GOOGLE_REVOKE_URL,
                data={'token': receipt['revocation_token']},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException:
            # Local deletion already committed. No later local write can erase
            # a reconnect while this best-effort provider request was in flight.
            # Google grant-wide revocation is not atomic with local reconnects.
            pass

    def get_status(self, user_id: str) -> Dict[str, Any]:
        connection = self.connection_db.get_connection(user_id)
        if not connection:
            return {'connected': False}
        return {
            'connected': True,
            'google_email': connection.get('google_email'),
            'connected_at': connection.get('created_at'),
        }

    # --- Token management ---

    @staticmethod
    def _expiry_iso(expires_in: Optional[int]) -> Optional[str]:
        if not expires_in:
            return None
        expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        return expiry.isoformat()

    def _get_valid_access_token(self, user_id: str) -> str:
        connection = self.connection_db.get_connection(user_id)
        if not connection:
            raise GSCError(
                code='gsc_not_connected',
                user_message='Connect Google Search Console first.',
                status_code=409,
            )

        expires_at = connection.get('token_expires_at')
        if expires_at:
            try:
                expiry = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
                if expiry - datetime.now(timezone.utc) > timedelta(seconds=60):
                    return connection['access_token']
            except ValueError:
                pass

        return self._refresh_access_token(user_id, connection['refresh_token'])

    def _refresh_access_token(self, user_id: str, refresh_token: str) -> str:
        unavailable = 'Search Console access could not be refreshed. Please try again later.'
        try:
            response = requests.post(
                GOOGLE_TOKEN_URL,
                data={
                    'refresh_token': refresh_token,
                    'client_id': Config.GOOGLE_OAUTH_CLIENT_ID,
                    'client_secret': Config.GOOGLE_OAUTH_CLIENT_SECRET,
                    'grant_type': 'refresh_token',
                },
                timeout=HTTP_TIMEOUT,
            )
            tokens = response.json()
        except (requests.RequestException, ValueError):
            raise GSCError('gsc_api_error', unavailable, 503) from None

        if not response.ok:
            # Never delete by owner after an external request: the connection
            # may have been replaced while Google was answering. A temporary
            # outage is also not evidence that a refresh token was revoked.
            if response.status_code == 400 and isinstance(tokens, dict) and tokens.get('error') == 'invalid_grant':
                raise GSCError(
                    'gsc_reauth_required',
                    'Search Console rejected the previous access. Reload connection status; reconnect if access is still unavailable.',
                    409,
                )
            raise GSCError('gsc_api_error', unavailable, 503)

        access_token = tokens.get('access_token') if isinstance(tokens, dict) else None
        if not isinstance(access_token, str) or not access_token or len(access_token) > 16384:
            raise GSCError('gsc_api_error', unavailable, 503)
        try:
            expires_at = self._expiry_iso(tokens.get('expires_in'))
        except (ValueError, TypeError, OverflowError):
            raise GSCError('gsc_api_error', unavailable, 503) from None
        try:
            # Reuse applied047, also used by AgentGSCProvider. Secret comparison
            # stays in the POST body, never a PostgREST URL. This RPC updates only
            # the connection whose refresh token produced this access token.
            updated = self.connection_db.client.rpc('refresh_migration_gsc_access_token', {
                'p_user_id': user_id, 'p_expected_refresh_token': refresh_token,
                'p_access_token': access_token, 'p_expires_at': expires_at,
            }).execute()
        except Exception:
            # An unconfirmed commit is not permission to issue another refresh
            # or to destroy credentials. No automatic mutation retry.
            raise GSCError('gsc_refresh_unconfirmed',
                'Token refresh could not be confirmed. Reload connection status before retrying.',503) from None
        if getattr(updated, 'data', None) is not True:
            raise GSCError('operation_conflict',
                'Your Search Console connection changed. Reload connection status before retrying.',409)
        return access_token

    # --- Search Console API ---

    def list_properties(self, user_id: str) -> List[Dict[str, Any]]:
        """List the user's verified GSC properties."""
        access_token = self._get_valid_access_token(user_id)
        response = requests.get(
            GSC_SITES_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=HTTP_TIMEOUT,
        )
        if not response.ok:
            logger.warning("GSC sites.list failed: %s", response.text[:500])
            raise GSCError(
                code='gsc_api_error',
                user_message='Could not load your Search Console properties. Please try again.',
                status_code=502,
            )

        entries = response.json().get('siteEntry', []) or []
        return [
            {'site_url': e['siteUrl'], 'permission_level': e.get('permissionLevel', '')}
            for e in entries
            if e.get('permissionLevel') != 'siteUnverifiedUser'
        ]

    def query_search_analytics(
        self,
        user_id: str,
        site_url: str,
        lookback_days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Pull per-page clicks/impressions for a property. Paginates until
        exhausted; returns [{url, clicks, impressions, ctr, position}, ...].
        """
        access_token = self._get_valid_access_token(user_id)
        days = lookback_days or Config.GSC_LOOKBACK_DAYS
        end_date = date.today() - timedelta(days=GSC_DATA_LAG_DAYS)
        start_date = end_date - timedelta(days=days)

        rows: List[Dict[str, Any]] = []
        start_row = 0
        while True:
            response = requests.post(
                GSC_QUERY_URL.format(site=requests.utils.quote(site_url, safe='')),
                headers={'Authorization': f'Bearer {access_token}'},
                json={
                    'startDate': start_date.isoformat(),
                    'endDate': end_date.isoformat(),
                    'dimensions': ['page'],
                    'rowLimit': SEARCH_ANALYTICS_ROW_LIMIT,
                    'startRow': start_row,
                },
                timeout=HTTP_TIMEOUT,
            )
            if not response.ok:
                logger.warning("GSC searchAnalytics.query failed: %s", response.text[:500])
                raise GSCError(
                    code='gsc_api_error',
                    user_message='Could not pull Search Console traffic data. Please try again.',
                    status_code=502,
                )

            batch = response.json().get('rows', []) or []
            for row in batch:
                keys = row.get('keys') or []
                if not keys:
                    continue
                rows.append({
                    'url': keys[0],
                    'clicks': int(row.get('clicks', 0)),
                    'impressions': int(row.get('impressions', 0)),
                    'ctr': float(row.get('ctr', 0.0)),
                    'position': float(row.get('position', 0.0)),
                })

            if len(batch) < SEARCH_ANALYTICS_ROW_LIMIT:
                break
            start_row += SEARCH_ANALYTICS_ROW_LIMIT

        return rows

    def sync_session(
        self,
        user_id: str,
        session: Dict[str, Any],
        site_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Pull Search Analytics data for a session's old URLs and store it.

        Auto-picks the property covering the session's old URLs when site_url
        is not supplied. Only metrics for URLs present in the session are kept.
        """
        old_urls = session.get('old_urls') or []
        if not old_urls:
            raise GSCError(
                code='gsc_no_old_urls',
                user_message='This project has no old URLs to look up in Search Console.',
                status_code=422,
            )

        if not site_url:
            properties = [p['site_url'] for p in self.list_properties(user_id)]
            site_url = pick_property_for_urls(properties, old_urls)
            if not site_url:
                raise GSCError(
                    code='gsc_no_matching_property',
                    user_message=(
                        'None of your verified Search Console properties cover the '
                        'old site in this project. Verify the old domain in Search '
                        'Console, then retry.'
                    ),
                    status_code=422,
                )

        analytics = self.query_search_analytics(user_id, site_url)

        session_urls = {normalize_url(u): u for u in old_urls}
        matched: Dict[str, Dict[str, Any]] = {}
        for row in analytics:
            key = normalize_url(row['url'])
            original = session_urls.get(key)
            if not original:
                continue
            existing = matched.get(original)
            if existing:
                # http/https or trailing-slash variants collapse to one URL.
                existing['clicks'] += row['clicks']
                existing['impressions'] += row['impressions']
            else:
                matched[original] = {**row, 'url': original}

        session_id = session['id']
        from uuid import UUID as _UUID
        session_uuid = session_id if not isinstance(session_id, str) else _UUID(session_id)
        stored = self.metrics_db.replace_session_metrics(session_uuid, list(matched.values()))
        self.metrics_db.set_session_gsc_metadata(session_uuid, site_url)

        total_clicks = sum(m['clicks'] for m in matched.values())
        total_impressions = sum(m['impressions'] for m in matched.values())
        return {
            'property': site_url,
            'matched_urls': stored,
            'session_url_count': len(old_urls),
            'total_clicks': total_clicks,
            'total_impressions': total_impressions,
        }

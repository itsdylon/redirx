"""
Deploy-ready redirect files, server-side.

Every export format lived in the React export modal, which meant the rules
could only be produced by a human with a browser open. An agent asked to "run
the migration and install the redirects" had no way to obtain the artifact —
the last step of the job was unreachable over HTTP. These are ports of the
client formatters, but safety takes precedence over byte compatibility: an
artifact that is syntactically valid but silently misroutes traffic is rejected.

Path handling is the subtlety. Apache, Nginx, Cloudflare, Shopify and Vercel
all match against the request *path*. The renderer emits exact Apache/Nginx
rules, rejects query-sensitive sources where a target cannot express them,
and rejects platform pattern syntax that would change a literal URL.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from typing import Any, Iterable, Optional
from urllib.parse import urlparse, urlunparse

APACHE = "apache"
NGINX = "nginx"
WORDPRESS = "wordpress"
VERCEL = "vercel"
CLOUDFLARE = "cloudflare"
SHOPIFY = "shopify"
CSV_FORMAT = "csv"
JSON_FORMAT = "json"

FORMATS = (
    APACHE,
    NGINX,
    WORDPRESS,
    VERCEL,
    CLOUDFLARE,
    SHOPIFY,
    CSV_FORMAT,
    JSON_FORMAT,
)

FILENAMES = {
    APACHE: "redirects.htaccess",
    NGINX: "redirects_nginx.conf",
    WORDPRESS: "redirects_wordpress.csv",
    VERCEL: "vercel.json",
    CLOUDFLARE: "_redirects",
    SHOPIFY: "shopify_redirects.csv",
    CSV_FORMAT: "redirects.csv",
    JSON_FORMAT: "redirects.json",
}

CONTENT_TYPES = {
    APACHE: "text/plain; charset=utf-8",
    NGINX: "text/plain; charset=utf-8",
    WORDPRESS: "text/csv; charset=utf-8",
    VERCEL: "application/json; charset=utf-8",
    CLOUDFLARE: "text/plain; charset=utf-8",
    SHOPIFY: "text/csv; charset=utf-8",
    CSV_FORMAT: "text/csv; charset=utf-8",
    JSON_FORMAT: "application/json; charset=utf-8",
}

# Formats whose matcher only ever sees the path. Emitting absolute URLs here
# produces a file that loads without error and silently redirects nothing.
PATH_ONLY_FORMATS = frozenset({APACHE, NGINX, CLOUDFLARE, SHOPIFY, VERCEL})


class UnknownExportFormat(ValueError):
    def __init__(self, fmt: str):
        super().__init__(
            f"Unknown export format '{fmt}'. Supported: {', '.join(FORMATS)}."
        )
        self.format = fmt


class ExportSelectionError(ValueError):
    """A mapping cannot be safely represented as a redirect rule."""


class UnsupportedExportInput(ExportSelectionError):
    """The selected platform cannot faithfully represent a mapping."""


def _safe_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if (any(ord(char) < 0x20 or ord(char) == 0x7F or char == "\\" for char in value)
            or any(char.isspace() for char in value)):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None):
            return None
        try:
            if parsed.hostname is None or parsed.port is not None and parsed.port < 0:
                return None
        except ValueError:
            return None
    elif not value.startswith("/"):
        return None
    return value


def _url_key(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme and not parsed.netloc:
        key = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    else:
        key = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(),
                         parsed.path or "/", parsed.params, parsed.query, ""))
    # Counting identity also preserves an explicitly empty query delimiter.
    return key + "?" if "?" in value.split("#", 1)[0] and not parsed.query else key


def _apache_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _nginx_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def _regex_path(value: str) -> str:
    parsed = urlparse(value)
    if parsed.netloc:
        path = parsed.path or "/"
        if parsed.params:
            path += f";{parsed.params}"
        return re.escape(path)
    return re.escape(value)


def _decision_exclusion(row: Mapping[str, Any]) -> Optional[str]:
    states = {
        str(row.get(field, "")).strip().lower()
        for field in ("status", "decision", "state", "mapping_status")
        if row.get(field) is not None
    }
    actions = {
        str(row.get(field, "")).strip().lower()
        for field in ("action", "decision_action")
        if row.get(field) is not None
    }
    if (states & {"held", "rejected", "pending", "needs_review", "unresolved",
                  "intentional_removal", "defer"}
            or actions & {"intentional_removal", "defer", "reject", "held"}):
        return "held_or_rejected"
    if row.get("approved") is False or row.get("validated") is False:
        return "held_or_rejected"
    if row.get("needs_review") is True:
        return "held_or_rejected"
    return None


def _effective_url(value: str, fallback_domain: Optional[str] = None) -> str:
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not fallback_domain:
        return value
    return rehost(value, fallback_domain)


def _comparison_pair(old_url: str, new_url: str, old_domain: Optional[str],
                     new_domain: Optional[str]) -> tuple[str, str]:
    effective_old = _effective_url(old_url, old_domain)
    fallback = new_domain or (
        f"{urlparse(effective_old).scheme}://{urlparse(effective_old).netloc}"
        if urlparse(effective_old).netloc else None
    )
    effective_new = _effective_url(new_url, fallback)
    if not urlparse(effective_old).netloc and urlparse(effective_new).netloc:
        effective_old = _effective_url(old_url, f"{urlparse(effective_new).scheme}://{urlparse(effective_new).netloc}")
    return effective_old, effective_new


def _render_pair(old_url: str, new_url: str, url_format: str,
                 old_domain: Optional[str], new_domain: Optional[str]) -> tuple[str, str]:
    old = _transform(old_url, url_format, old_domain)
    new = _transform(new_url, url_format, new_domain)
    effective_old, effective_new = _comparison_pair(old_url, new_url, old_domain, new_domain)
    if url_format == "paths" and _origin(effective_old) != _origin(effective_new):
        # A path-only source can still redirect to another host.  Dropping the
        # destination origin would turn a domain move into an internal link.
        new = rehost(new_url, new_domain) if new_domain and not urlparse(new_url).netloc else _transform(new_url, "full", new_domain)
    return old, new


def _origin(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    return parsed.scheme.lower(), parsed.netloc.lower()


def select_export_mappings(
    mappings: Iterable[dict[str, Any]],
    *,
    url_format: str = "paths",
    old_domain: Optional[str] = None,
    new_domain: Optional[str] = None,
) -> dict[str, Any]:
    """Select safe redirect rules and report every excluded row.

    Rows with no decision fields remain accepted for compatibility with the
    legacy export endpoint.  When decision fields exist, explicit holds,
    rejections, review flags, and validation failures are never exported.
    """
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for index, row in enumerate(mappings):
        if not isinstance(row, Mapping):
            exclusions.append({"index": index, "reason": "invalid_mapping"})
            continue
        old_raw = row.get("old_url") or row.get("oldUrl")
        new_raw = row.get("new_url") or row.get("newUrl")
        old_url = _safe_url(old_raw)
        new_url = _safe_url(new_raw)
        if old_url is None or new_url is None:
            exclusions.append({"index": index, "reason": "invalid_url"})
            continue
        decision_reason = _decision_exclusion(row)
        if decision_reason:
            exclusions.append({"index": index, "reason": decision_reason})
            continue
        effective_old, effective_new = _comparison_pair(old_url, new_url, old_domain, new_domain)
        if _url_key(effective_old) == _url_key(effective_new):
            exclusions.append({"index": index, "reason": "no_op"})
            continue
        old_rendered, new_rendered = _render_pair(old_url, new_url, url_format, old_domain, new_domain)
        candidates.append({
            "index": index,
            "row": dict(row),
            "old_url": old_url,
            "new_url": new_url,
            "old_rendered": old_rendered,
            "new_rendered": new_rendered,
            "effective_old": effective_old,
            "effective_new": effective_new,
        })

    unique: list[dict[str, Any]] = []
    seen_rules: set[tuple[str, str]] = set()
    for item in candidates:
        rule_key = (item["old_rendered"], item["new_rendered"])
        if rule_key in seen_rules:
            exclusions.append({"index": item["index"], "reason": "duplicate_rule"})
        else:
            seen_rules.add(rule_key)
            unique.append(item)
    candidates = unique

    by_source: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_source.setdefault(candidate["old_rendered"], []).append(candidate)
    kept: list[dict[str, Any]] = []
    for source, same_source in by_source.items():
        destinations = {item["new_rendered"] for item in same_source}
        if len(destinations) > 1:
            for item in same_source:
                exclusions.append({"index": item["index"], "reason": "conflicting_source"})
        else:
            kept.extend(same_source)

    # Exclude only actual cycles.  A chain is not silently rewritten; its
    # explicit rules remain inspectable and can be verified independently.
    edges = {_url_key(item["effective_old"]): _url_key(item["effective_new"]) for item in kept}
    cycle_nodes: set[str] = set()
    state: dict[str, int] = {}
    for start in edges:
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in edges and state.get(current, 0) == 0:
            state[current] = 1
            positions[current] = len(path)
            path.append(current)
            current = edges[current]
        if state.get(current) == 1 and current in positions:
            cycle_nodes.update(path[positions[current]:])
        for node in path:
            state[node] = 2
    selected: list[dict[str, Any]] = []
    for item in kept:
        if _url_key(item["effective_old"]) in cycle_nodes:
            exclusions.append({"index": item["index"], "reason": "redirect_loop"})
        else:
            selected.append(item)
    selected.sort(key=lambda item: item["index"])
    exclusions.sort(key=lambda item: (item["index"], item["reason"]))
    return {
        "mappings": selected,
        "excluded": exclusions,
        "included_count": len(selected),
        "excluded_count": len(exclusions),
    }


def to_path(url: str) -> str:
    """
    The path portion of a URL, or the input unchanged if it is not parseable.

    Returning the input rather than raising matches the UI: a row that is
    already a bare path ("/pricing") must survive untouched.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if not parsed.scheme or not parsed.netloc:
        return url
    # Slice the raw suffix rather than reconstructing parsed components. URL
    # parsers discard empty ?/# delimiters; target fragments affect navigation.
    suffix = url[len(parsed.scheme) + 3 + len(parsed.netloc):]
    return suffix if suffix.startswith("/") else "/" + suffix


def rehost(url: str, domain: str) -> str:
    """Swap a URL onto another origin, preserving path and query."""
    try:
        parsed = urlparse(url)
        target = urlparse(domain if "://" in domain else f"https://{domain}")
    except Exception:
        return url
    if not target.netloc:
        return url
    suffix = (url[len(parsed.scheme) + 3 + len(parsed.netloc):]
              if parsed.scheme and parsed.netloc else url)
    return f"{target.scheme or 'https'}://{target.netloc}" + suffix


def _transform(
    url: str,
    url_format: str,
    domain: Optional[str],
) -> str:
    if url_format == "full":
        return url
    if url_format == "custom" and domain:
        return rehost(url, domain)
    return to_path(url)




def _csv_rows(header: list[str], rows: list[list[str]]) -> str:
    """
    Real CSV quoting rather than comma-joining.

    The client builds these by string concatenation, which corrupts any URL
    containing a comma. Writing them properly is a strict improvement and
    still byte-identical for the overwhelming majority of rows, which contain
    no quotable characters.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if header:
        writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().rstrip("\n")


def build_export(
    mappings: Iterable[dict[str, Any]],
    fmt: str,
    *,
    url_format: str = "paths",
    old_domain: Optional[str] = None,
    new_domain: Optional[str] = None,
) -> str:
    """
    Render matches as a deploy-ready redirect file.

    Args:
        mappings: rows with old_url/new_url (camelCase accepted too).
        fmt: one of FORMATS.
        url_format: 'paths' (default), 'full', or 'custom' with a domain.
    """
    _validate_export_format(fmt, url_format)

    selection = select_export_mappings(
        mappings,
        url_format=url_format,
        old_domain=old_domain,
        new_domain=new_domain,
    )
    return _render_export_selection(selection, fmt, url_format=url_format)


def _validate_export_format(fmt: str, url_format: str) -> None:
    if fmt not in FORMATS:
        raise UnknownExportFormat(fmt)
    if fmt in PATH_ONLY_FORMATS and url_format != "paths":
        raise UnsupportedExportInput(
            f"{fmt} requires url_format='paths' for an exact, deployable source rule."
        )


def _render_export_selection(selection: Mapping[str, Any], fmt: str, *, url_format: str) -> str:
    """Render a same-request selection produced by select_export_mappings.

    Internal only: callers must not supply HTTP/client-selected rows. The artifact
    service already uses this exact selection for counts and verification inputs;
    reusing it here avoids rescanning without weakening platform safety checks.
    """
    _validate_export_format(fmt, url_format)
    selected = selection["mappings"]
    if fmt == APACHE and any("$" in item["new_url"] for item in selected):
        raise UnsupportedExportInput(
            "Apache RedirectMatch cannot safely represent a literal '$' in a destination."
        )
    if fmt == NGINX and any("$" in item["old_rendered"] or "$" in item["new_rendered"] for item in selected):
        raise UnsupportedExportInput(
            "Nginx exact location fragments do not support literal '$' safely."
        )
    if fmt in {VERCEL, CLOUDFLARE}:
        pattern_chars = (":", "*", "(", ")", "[", "]", "{", "}")
        if any(any(char in item["old_rendered"] for char in pattern_chars) for item in selected):
            raise UnsupportedExportInput(
                f"{fmt} treats pattern syntax in source paths as a matcher, not a literal URL."
            )
    if fmt not in {CSV_FORMAT, JSON_FORMAT} and any("#" in item["effective_old"] for item in selected):
        raise UnsupportedExportInput(
            "Source fragments are not sent to the server and cannot select a redirect rule. "
            "Review the source URL or export CSV/JSON for manual handling."
        )
    if fmt in {APACHE, NGINX} and any(
        "%" in (path := urlparse(item["effective_old"]).path)
        or "//" in path or any(segment in {".", ".."} for segment in path.split("/"))
        for item in selected
    ):
        raise UnsupportedExportInput(
            f"{fmt} normalizes encoded paths, repeated slashes and dot segments before matching. "
            "These exact raw source URLs require custom server rules; export CSV/JSON for review."
        )
    if fmt in PATH_ONLY_FORMATS and any("?" in item["effective_old"].split("#", 1)[0] for item in selected):
        raise UnsupportedExportInput(
            "This platform cannot express query-sensitive source rules safely."
        )
    pairs = [
        (item["old_rendered"], item["new_rendered"])
        for item in selected
    ]

    if fmt == APACHE:
        return "\n".join(
            f'RedirectMatch 301 "^{_apache_literal(_regex_path(item["old_rendered"]))}$" '
            f'"{_apache_literal(item["new_rendered"])}"'
            for item in selected
        )

    if fmt == NGINX:
        if not selected:
            return "# No redirect rules generated."
        body = "\n".join(
            f'location = "{_nginx_literal(item["old_rendered"])}" {{ '
            f'return 301 "{_nginx_literal(item["new_rendered"])}"; }}'
            for item in selected
        )
        return "# Include inside the target server block.\n" + body

    if fmt == WORDPRESS:
        # Redirection CSV: source, target, regex, HTTP code, type.
        # A third-column 301 is interpreted as the regex flag, not the status.
        return _csv_rows([], [[old, new, "0", "301", "url"] for old, new in pairs])

    if fmt == VERCEL:
        return json.dumps(
            {
                "redirects": [
                    {"source": old, "destination": new, "permanent": True}
                    for old, new in pairs
                ]
            },
            indent=2,
        )

    if fmt == CLOUDFLARE:
        # Pages _redirects has separate static/dynamic limits. These exports
        # contain literal static rules only; never truncate an approved artifact.
        lines = [f"{old} {new} 301" for old, new in pairs]
        if len(lines) > 2000 or any(len(line) > 1000 for line in lines):
            raise UnsupportedExportInput(
                "Cloudflare Pages supports at most 2,000 static rules and 1,000 characters per rule. "
                "Export CSV/JSON for review and use an appropriate Bulk Redirects import or custom configuration."
            )
        return "\n".join(lines)

    if fmt == SHOPIFY:
        return _csv_rows(["Redirect from", "Redirect to"], [[o, n] for o, n in pairs])

    if fmt == CSV_FORMAT:
        return _csv_rows(["old_url", "new_url", "status"], [[o, n, "301"] for o, n in pairs])

    # JSON
    return json.dumps(
        [{"from": old, "to": new, "status": 301} for old, new in pairs],
        indent=2,
    )


def filename_for(fmt: str) -> str:
    if fmt not in FORMATS:
        raise UnknownExportFormat(fmt)
    return FILENAMES[fmt]


def content_type_for(fmt: str) -> str:
    if fmt not in FORMATS:
        raise UnknownExportFormat(fmt)
    return CONTENT_TYPES[fmt]


def warning_for(fmt: str, url_format: str) -> Optional[str]:
    """
    Why a chosen combination will not work, if it will not.

    Returned for platform requirements that are actionable without changing
    the generated artifact. Unsupported combinations are raised by
    ``build_export`` instead of emitting a misleading file.
    """
    if fmt == NGINX:
        return (
            "Nginx output is a server-block fragment; merge its exact location "
            "rules into the existing target server configuration."
        )
    if fmt == CLOUDFLARE:
        return (
            "This is a Cloudflare Pages _redirects file; Cloudflare Workers or "
            "other products require their own route configuration."
        )
    if url_format != "paths" and fmt in PATH_ONLY_FORMATS:
        return (
            f"{fmt} matches against the request path — absolute URLs in the "
            "source will never match. Use url_format='paths'."
        )
    return None

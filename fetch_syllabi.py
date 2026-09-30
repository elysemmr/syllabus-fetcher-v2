#!/usr/bin/env python3
"""Download course syllabi from Brightspace (D2L) for a batch of course codes.

Login happens through a real, visible browser window so you can complete your
school's SSO flow (including MFA) by hand. After that, the script tries to
find and download each syllabus automatically via Brightspace's own REST
API (no clicking needed); if that doesn't pan out for some reason, it falls
back to searching the content tree and clicking through the UI itself, and
as a last resort, asks you to click the link so it can capture the result.

Usage:
    python fetch_syllabi.py --courses CSE201,MATH150,ENGL101
    python fetch_syllabi.py --courses-file courses.txt --output-dir ~/Desktop/Syllabi

    # Fetching on behalf of someone else, from a loosely-formatted list they
    # sent you (requires an admin-level Brightspace account):
    python fetch_syllabi.py --requester jsmith123 --courses-file their_list.txt

See README.md for one-time setup (installing Playwright's browser, filling
in config.json).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from html import unescape
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import (
    BrowserContext,
    Download,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LOG = logging.getLogger("fetch_syllabi")

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "Syllabi"
SESSION_DIR = PROJECT_DIR / ".auth" / "browser-profile"

LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # generous window to click through SSO/MFA by hand
NAV_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 20_000

SYLLABUS_RE = re.compile(r"syllabus", re.IGNORECASE)

# Extensions Word can open and convert to PDF via docx2pdf. Anything else
# downloaded (a spreadsheet, an image, a zip, ...) is left as-is with a
# warning.
WORD_CONVERTIBLE_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt"}

# Brightspace/D2L instances are re-themed per school, so these selectors are
# best-effort defaults, tried in order. If your MyFire/Brightspace theme uses
# different markup, tweak this list -- everything else in the script is
# selector-agnostic and falls back to asking you to click manually.
COURSE_SELECTOR_BUTTON_CANDIDATES = [
    'button[aria-label^="Select a course"]',  # confirmed via inspecting MyFire
    '[title="Select a course"]',
    'button:has-text("Select a course")',
    'd2l-navigation-main-header >> [title="Course Selector"]',
]
COURSE_SEARCH_INPUT_CANDIDATES = [
    'input[placeholder="Search for a course"]',  # confirmed exact text on MyFire's course selector
    'input[type="search"]',
    'input[placeholder*="ourse" i]',
]
EXPAND_ALL_CANDIDATES = [
    'button:has-text("Expand All")',
    '[title="Expand All"]',
]
DOWNLOAD_BUTTON_CANDIDATES = [
    'button:has-text("Download")',
    'a:has-text("Download")',
    '[title="Download"]',
]
CONTENT_NAV_LINK_CANDIDATES = [
    'a:has-text("Content")',
]


class CourseNotFound(Exception):
    pass


class SyllabusNotFound(Exception):
    pass


# --- Brightspace REST API auto-detection ------------------------------------
#
# Brightspace's own web UI is built by calling its documented REST API
# (https://docs.valence.desire2learn.com/), and that API is reachable with
# the same session cookies the logged-in browser already has. Querying it
# directly for the course's content structure and the syllabus file is far
# more reliable than guessing at rendered HTML/CSS, and needs zero clicking.
# Everything below is best-effort: if anything about it doesn't match this
# Brightspace instance, it returns None/raises nothing, and the caller falls
# back to the click-based flow further down in this file.

def get_api_versions(context: BrowserContext, base_url: str) -> dict[str, str]:
    """Look up the latest supported version of each Brightspace API product
    (e.g. "le", "lp"), needed to build correct API URLs."""
    versions: dict[str, str] = {}
    try:
        resp = context.request.get(f"{base_url.rstrip('/')}/d2l/api/versions/")
        if resp.status != 200:
            LOG.debug("API versions lookup returned HTTP %d: %s", resp.status, resp.text()[:500])
            return versions
        for entry in resp.json():
            code = entry.get("ProductCode")
            latest = entry.get("LatestVersion")
            if code and latest:
                versions[code] = latest
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Couldn't fetch Brightspace API versions.", exc_info=True)
    LOG.debug("Detected API versions: %s", versions)
    return versions


ORG_UNIT_URL_RE = re.compile(r"/d2l/le/content/(\d+)")


def extract_org_unit_id(page: Page) -> int | None:
    """Read the numeric org unit ID straight out of the current page's URL,
    once the course-selector UI has already navigated to the course's
    Content page. Far more reliable than independently re-deriving it by
    matching the course code against a potentially huge enrollment history."""
    match = ORG_UNIT_URL_RE.search(page.url)
    return int(match.group(1)) if match else None


# (org unit id, code, name) for every course enrollment, fetched once per
# run: the list can be tens of thousands of entries long, and Brightspace
# ignores the `search` parameter on this endpoint, so it can only be paged.
_enrollment_cache: dict[str, list[tuple[int, str, str]]] = {}


def _all_course_enrollments(
    context: BrowserContext, base_url: str, lp_version: str
) -> list[tuple[int, str, str]]:
    if base_url in _enrollment_cache:
        return _enrollment_cache[base_url]
    root = base_url.rstrip("/")
    url: str | None = f"{root}/d2l/api/lp/{lp_version}/enrollments/myenrollments/?orgUnitTypeId=3&pageSize=100"
    enrollments: list[tuple[int, str, str]] = []
    pages = 0
    while url:
        # A 30s cap per page: without it, one stalled request hangs the
        # whole run with no way to skip it, since this isn't interruptible
        # by the usual per-course "press Enter" prompts.
        resp = context.request.get(url, timeout=30_000)
        if resp.status != 200:
            raise RuntimeError(f"Enrollment list returned HTTP {resp.status}: {resp.text()[:300]}")
        data = resp.json()
        for item in data.get("Items", []):
            org_unit = item.get("OrgUnit", {})
            if org_unit.get("Id") is not None:
                enrollments.append((org_unit["Id"], org_unit.get("Code") or "", org_unit.get("Name") or ""))
        pages += 1
        if pages % 10 == 0:
            LOG.info("Scanning enrollments... %d so far.", len(enrollments))
        paging = data.get("PagingInfo", {})
        url = (
            f"{root}/d2l/api/lp/{lp_version}/enrollments/myenrollments/"
            f"?orgUnitTypeId=3&pageSize=100&bookmark={paging['Bookmark']}"
            if paging.get("HasMoreItems")
            else None
        )
    _enrollment_cache[base_url] = enrollments
    return enrollments


def find_org_unit_id_via_api(
    context: BrowserContext, base_url: str, api_versions: dict[str, str], course_code: str
) -> int | None:
    """Look a course up in the user's own enrollment list when the
    course-selector UI can't find it (it doesn't list older courses).
    Matches if the given text appears anywhere in the real code or name,
    ignoring separators/casing -- so "ENGL_2133_31_FX" finds
    "2026_US_ENGL_2133_31_FX_BSIDE" without needing the year or site suffix
    typed out too -- and only if it identifies exactly one course. Returns
    None (never raises) otherwise."""
    lp_version = api_versions.get("lp")
    if not lp_version:
        return None
    target = normalize_code(course_code)
    if not target:
        return None
    try:
        enrollments = _all_course_enrollments(context, base_url, lp_version)
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Couldn't list enrollments.", exc_info=True)
        return None

    def matches(text: str) -> bool:
        return target in normalize_code(text)

    hits = {
        org_unit_id: (code, name.strip())
        for org_unit_id, code, name in enrollments
        if matches(code) or matches(name)
    }
    if len(hits) == 1:
        return next(iter(hits))
    LOG.info(
        "Enrollment list had %d exact matches for %s%s.",
        len(hits), course_code, f": {sorted(hits.items())}" if hits else "",
    )
    return None


def _filename_from_response(resp, fallback_url: str) -> str:
    content_disposition = resp.headers.get("content-disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
    if match:
        return unquote(match.group(1))
    return Path(fallback_url.split("?")[0]).name or "syllabus.pdf"


# The link text often wraps "syllabus" in another tag rather than holding it
# directly ("<a href=...><span>Course Syllabus</span></a>"), so allow any
# tags in between, not just plain text -- just don't cross into the next
# <a>...</a> pair (the lazy match stops at the first </a> it reaches).
SYLLABUS_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(?:(?!</a>).)*?syllabus(?:(?!</a>).)*?</a>',
    re.IGNORECASE | re.DOTALL,
)

GOOGLE_DOC_RE = re.compile(r"docs\.google\.com/document/d/([\w-]+)")
# The path of an uploaded file as Brightspace's file viewer page embeds it in
# its script (the iframe that shows the PDF is loaded from JS, not from HTML).
# Reached either directly ("viewFile.d2lfile/...") or via an intermediate LOR
# viewer page ("view.d2l?ou=...&loIdentId=...") that embeds this same link.
VIEW_FILE_RE = re.compile(r"/d2l/lor/viewer/viewFile\.d2lfile/[^'\"\s)<>]+")


def _resolve_viewer_page(
    context: BrowserContext, base_url: str, resp, title: str, notes: list[str] | None = None
) -> tuple[bytes, str] | None:
    """Given a successful GET response that might be the real file or an
    HTML page wrapping it, return the actual file's (bytes, filename): the
    response as-is if it's already a file, or, if it's HTML, whatever's
    inside it -- a Google Doc (exported as PDF) or a Brightspace file viewer
    link (named in the page's script, not in any plain href)."""
    if "html" not in resp.headers.get("content-type", "").lower():
        return resp.body(), _filename_from_response(resp, unquote(resp.url))

    doc = GOOGLE_DOC_RE.search(resp.url)
    if doc:
        export = context.request.get(f"https://docs.google.com/document/d/{doc.group(1)}/export?format=pdf")
        body = export.body() if export.status == 200 else b""
        if body.startswith(b"%PDF"):
            LOG.debug("Exported Google Doc %s as a PDF (%d bytes).", doc.group(1), len(body))
            if notes is not None:
                notes.append("live Google Doc, may differ from that term")
            return body, _filename_from_response(export, f"{title.replace('/', '-')}.pdf")
        LOG.debug(
            "Google Doc export of %s returned HTTP %d (%d bytes, not a PDF).",
            doc.group(1), export.status, len(body),
        )
        return None

    match = VIEW_FILE_RE.search(resp.text())
    if match:
        file_url = f"{base_url.rstrip('/')}{match.group(0)}"
        file_resp = context.request.get(file_url)
        if file_resp.status == 200:
            LOG.debug("Downloaded viewer file %s (%d bytes).", file_url, len(file_resp.body()))
            return file_resp.body(), _filename_from_response(file_resp, unquote(file_url))
        LOG.debug("Viewer file %s returned HTTP %d.", file_url, file_resp.status)
    else:
        LOG.debug("%r's page is HTML with no Google Doc or viewer file in it.", title)
    return None


def _iter_module_descriptions(modules: list[dict], parents: tuple[str, ...] = ()):
    """Yield (module path, description HTML) for every module that has a
    description. A syllabus link is sometimes in a module's intro text
    ("Review <a>Course Syllabus & Policies</a>") rather than in any topic."""
    for module in modules or []:
        path = parents + (module.get("Title") or "",)
        description = module.get("Description")
        html = description.get("Html") if isinstance(description, dict) else description
        if isinstance(html, str) and html:
            yield " > ".join(part for part in path if part), html
        yield from _iter_module_descriptions(module.get("Modules", []) or [], path)


def _download_syllabus_href(
    context: BrowserContext, base_url: str, href: str, notes: list[str] | None = None
) -> tuple[bytes, str] | None:
    """Download the file a syllabus link points at (relative or absolute),
    unwrapping it first if the link actually leads to an HTML viewer page
    (a Brightspace LOR viewer or a Google Doc) rather than the file itself."""
    href = unescape(href)
    file_url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"
    try:
        file_resp = context.request.get(file_url)
        if file_resp.status != 200:
            LOG.debug("Fetching syllabus file %r returned HTTP %d.", file_url, file_resp.status)
            return None
        result = _resolve_viewer_page(context, base_url, file_resp, "syllabus", notes)
        if result:
            LOG.debug("Downloaded syllabus %r (%d bytes) from %s", result[1], len(result[0]), file_url)
        else:
            LOG.debug("Syllabus link %r led to an HTML page with no file in it.", file_url)
        return result
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Fetching syllabus file %r failed.", file_url, exc_info=True)
    return None


def _iter_toc_topics(modules: list[dict], parents: tuple[str, ...] = ()):
    """Yield every topic in the table of contents, each with a "ModulePath"
    like "Reference Information > Week 1" saying where it sits."""
    for module in modules or []:
        path = parents + (module.get("Title") or "",)
        for topic in module.get("Topics", []) or []:
            yield {**topic, "ModulePath": " > ".join(part for part in path if part)}
        yield from _iter_toc_topics(module.get("Modules", []) or [], path)


# Why the last syllabus search in each course (by org unit ID) came up empty,
# for the summary. Filled in by find_syllabus_via_api.
_search_failure_reason: dict[int, str] = {}
# A caveat about where a found syllabus came from (e.g. a live Google Doc),
# by org unit ID, for the summary.
_source_note: dict[int, str] = {}


def find_syllabus_via_api(
    context: BrowserContext, base_url: str, le_version: str, org_unit_id: int
) -> tuple[bytes, str] | None:
    try:
        resp = context.request.get(
            f"{base_url.rstrip('/')}/d2l/api/le/{le_version}/{org_unit_id}/content/toc"
        )
        if resp.status != 200:
            LOG.debug("Content TOC lookup returned HTTP %d: %s", resp.status, resp.text()[:500])
            _search_failure_reason[org_unit_id] = f"content list returned HTTP {resp.status}"
            return None
        modules = resp.json().get("Modules", []) or []
        topics = list(_iter_toc_topics(modules))
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Content TOC lookup failed.", exc_info=True)
        _search_failure_reason[org_unit_id] = "content list unreadable"
        return None
    # The search reads File, Link and HTML topics only; say so when others exist.
    unsearched = [
        topic for topic in topics
        if not (
            (topic.get("TypeIdentifier") or "").lower() == "file"
            or "html" in (topic.get("TypeIdentifier") or "").lower()
            or (topic.get("TypeIdentifier") or "").lower() == "link"
        )
    ]
    if not topics:
        _search_failure_reason[org_unit_id] = "course has no content"
    else:
        reason = f"not in {len(topics) - len(unsearched)} searchable topics or module descriptions"
        if unsearched:
            types = sorted({topic.get("TypeIdentifier") or "unknown" for topic in unsearched})
            reason += f"; {len(unsearched)} unsearched ({', '.join(types)})"
        _search_failure_reason[org_unit_id] = reason
    _source_note.pop(org_unit_id, None)
    undownloadable: list[str] = []

    LOG.debug(
        "Content TOC has %d topics: %s",
        len(topics),
        [(t.get("Title"), t.get("TypeIdentifier")) for t in topics],
    )

    # Fast path: a topic whose own title says "syllabus" -- an uploaded file,
    # or a Link topic wrapping a file or a Google Doc.
    for topic in topics:
        if SYLLABUS_RE.search(topic.get("Title") or ""):
            LOG.debug(
                "Topic title matches 'syllabus' directly: %r (in %r)",
                topic.get("Title"), topic.get("ModulePath"),
            )
            source_notes: list[str] = []
            result = (
                _fetch_topic_file(context, base_url, le_version, org_unit_id, topic)
                or _download_from_topic_url(context, base_url, topic, source_notes)
            )
            if result:
                _source_note[org_unit_id] = "; ".join(source_notes)
                LOG.info("Syllabus is the topic %r (in %r).", topic.get("Title"), topic.get("ModulePath"))
                if source_notes:
                    LOG.info("Note: %s.", "; ".join(source_notes))
                return result
            LOG.debug("Fetching topic file for %r failed.", topic.get("Title"))
            undownloadable.append(topic.get("Title") or "")

    # A syllabus link in a module's description, which is already in the TOC.
    for module_path, description_html in _iter_module_descriptions(modules):
        match = SYLLABUS_LINK_RE.search(description_html)
        if not match:
            continue
        LOG.debug("Module %r's description links to a syllabus: %s", module_path, match.group(1))
        source_notes = []
        result = _download_syllabus_href(context, base_url, match.group(1), source_notes)
        if result:
            _source_note[org_unit_id] = "; ".join(source_notes)
            LOG.info("Syllabus is a link in the description of the module %r.", module_path)
            if source_notes:
                LOG.info("Note: %s.", "; ".join(source_notes))
            return result

    # Slower path: a syllabus link buried inside a content page's body --
    # either an uploaded "File" topic (often an HTML page, like a "Course
    # Resources" checklist) or a "Link"/HTML topic reachable via its own Url.
    for topic in topics:
        type_id = (topic.get("TypeIdentifier") or "").lower()
        html: str | None = None

        if type_id == "file":
            file_result = _fetch_topic_file(context, base_url, le_version, org_unit_id, topic)
            if not file_result:
                LOG.debug("Couldn't fetch File-type topic %r.", topic.get("Title"))
                continue
            content_bytes, _filename = file_result
            try:
                html = content_bytes.decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001 - best-effort
                continue
        elif "html" in type_id or type_id == "link":
            url = topic.get("Url")
            if not url:
                continue
            try:
                full_url = url if url.startswith("http") else f"{base_url.rstrip('/')}{url}"
                page_resp = context.request.get(full_url)
                if page_resp.status != 200:
                    LOG.debug(
                        "Fetching topic page %r (%s) returned HTTP %d.",
                        topic.get("Title"), full_url, page_resp.status,
                    )
                    continue
                html = page_resp.text()
            except Exception:  # noqa: BLE001 - best-effort
                LOG.debug("Fetching topic page %r failed.", topic.get("Title"), exc_info=True)
                continue
        else:
            continue

        if not html:
            continue
        match = SYLLABUS_LINK_RE.search(html)
        if not match:
            LOG.debug(
                "No syllabus link found in topic %r (%d chars of HTML). "
                "Any 'syllabus' mentions: %s. Start of content: %r",
                topic.get("Title"), len(html),
                [html[max(m.start() - 80, 0):m.end() + 80] for m in SYLLABUS_RE.finditer(html)][:5],
                html[:300],
            )
            continue
        LOG.debug("Syllabus link found in topic %r: %s", topic.get("Title"), match.group(1))
        source_notes = []
        result = _download_syllabus_href(context, base_url, match.group(1), source_notes)
        if result:
            _source_note[org_unit_id] = "; ".join(source_notes)
            LOG.info("Syllabus is a link inside the topic %r (in %r).", topic.get("Title"), topic.get("ModulePath"))
            if source_notes:
                LOG.info("Note: %s.", "; ".join(source_notes))
            return result

    if undownloadable:
        _search_failure_reason[org_unit_id] = (
            f"syllabus topic {', '.join(repr(title) for title in undownloadable)} won't download"
        )
    return None


def _fetch_topic_file(
    context: BrowserContext, base_url: str, le_version: str, org_unit_id: int, topic: dict
) -> tuple[bytes, str] | None:
    # The content TOC endpoint names a topic's numeric ID "TopicId" ("Id" is
    # what the single-topic endpoints use), so accept either.
    topic_id = topic.get("TopicId") or topic.get("Id")
    if topic_id is None:
        LOG.debug(
            "Topic %r has no TopicId/Id, can't fetch its file. Keys present: %s",
            topic.get("Title"), sorted(topic),
        )
        return None
    url = f"{base_url.rstrip('/')}/d2l/api/le/{le_version}/{org_unit_id}/content/topics/{topic_id}/file"
    try:
        resp = context.request.get(url)
        if resp.status != 200:
            LOG.debug(
                "Fetching topic file %r (%s) returned HTTP %d: %s",
                topic.get("Title"), url, resp.status, resp.text()[:300],
            )
            return None
        return resp.body(), _filename_from_response(resp, topic.get("Title") or "syllabus.pdf")
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Fetching topic file %r (%s) raised an exception.", topic.get("Title"), url, exc_info=True)
        return None


def _download_from_topic_url(
    context: BrowserContext, base_url: str, topic: dict, notes: list[str] | None = None
) -> tuple[bytes, str] | None:
    """For a topic that isn't an uploaded file (so its /file endpoint 404s),
    follow its Url and pull the document out of what that points at."""
    title = topic.get("Title") or "syllabus"
    url = topic.get("Url")
    if not url:
        LOG.debug("Topic %r has no Url to follow.", title)
        return None
    full_url = url if url.startswith("http") else f"{base_url.rstrip('/')}{url}"
    try:
        resp = context.request.get(full_url)
        if resp.status != 200:
            LOG.debug("Following topic %r (%s) returned HTTP %d.", title, full_url, resp.status)
            return None
        result = _resolve_viewer_page(context, base_url, resp, title, notes)
        if not result:
            LOG.debug("Topic %r's page names no Google Doc or viewer file.", title)
        return result
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Following topic %r (%s) raised an exception.", title, full_url, exc_info=True)
    return None


def try_api_auto_download(
    context: BrowserContext, base_url: str, api_versions: dict[str, str], org_unit_id: int
) -> tuple[bytes, str] | None:
    """Best-effort content search via Brightspace's REST API for a course
    whose org unit ID is already known (read from the URL after the
    course-selector UI has navigated there). Returns None (never raises) on
    any failure so the caller can fall back to the click-based flow."""
    le_version = api_versions.get("le")
    if not le_version:
        return None
    try:
        return find_syllabus_via_api(context, base_url, le_version, org_unit_id)
    except Exception:  # noqa: BLE001 - this whole path is best-effort
        LOG.debug("API auto-download attempt failed.", exc_info=True)
        return None


def normalize_code(s: str | None) -> str:
    """Strip everything but letters/digits and uppercase, so 'PSYC 4063',
    'psyc-4063', and '..._PSYC_4063_...' all compare equal."""
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def find_user_id_by_username(context: BrowserContext, base_url: str, lp_version: str, username: str) -> int | None:
    """Look up a Brightspace user's internal ID from their username, via the
    admin-level user search API."""
    try:
        resp = context.request.get(
            f"{base_url.rstrip('/')}/d2l/api/lp/{lp_version}/users/",
            params={"userName": username},
        )
        if resp.status != 200:
            LOG.debug("User lookup for %r returned HTTP %d: %s", username, resp.status, resp.text()[:500])
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("User lookup for %r failed.", username, exc_info=True)
        return None

    # Different API versions return either a single object or a list.
    if isinstance(data, list):
        if not data:
            LOG.debug("No user found for username %r.", username)
            return None
        data = data[0]
    user_id = data.get("UserId") or data.get("Identifier")
    LOG.debug("Resolved username %r to user ID %s.", username, user_id)
    return user_id


def get_user_enrollments(
    context: BrowserContext,
    base_url: str,
    lp_version: str,
    user_id: int,
    student_role_id: int | None = None,
) -> tuple[list[dict], dict[int, str]]:
    """Fetch every course (org unit) a given user is enrolled in *as a
    student*, as a list of {"Id", "Code", "Name"} dicts, via the
    admin-level enrollments API. Each enrollment also carries a Role (e.g.
    "Student", "TA", "Administrator Dev").

    If student_role_id is given (see config.json's student_role_id), the
    API itself is asked to only return that role, which is much faster for
    an account with access to many/all courses (e.g. an admin account) --
    otherwise every enrollment has to be fetched and filtered by role name
    here instead, which means paging through all of them.

    Also returns whatever {role id: role name} pairs were seen among the
    student-role enrollments, so the caller can save student_role_id to
    config.json automatically when it wasn't already set."""
    enrollments: list[dict] = []
    skipped_non_student = 0
    seen_roles: dict[int, str] = {}
    bookmark: str | None = None
    for page_num in range(500):  # safety cap on pagination
        params = {"orgUnitTypeId": "3"}  # course offerings only, not departments/semesters/templates
        if student_role_id is not None:
            params["roleId"] = str(student_role_id)
        if bookmark:
            params["bookmark"] = bookmark
        try:
            resp = context.request.get(
                f"{base_url.rstrip('/')}/d2l/api/lp/{lp_version}/enrollments/users/{user_id}/orgUnits/",
                params=params,
            )
            if resp.status != 200:
                LOG.debug(
                    "Enrollment fetch for user %s (page %d) returned HTTP %d: %s",
                    user_id, page_num, resp.status, resp.text()[:500],
                )
                break
            data = resp.json()
        except Exception:  # noqa: BLE001 - best-effort
            LOG.debug("Enrollment fetch for user %s failed.", user_id, exc_info=True)
            break

        for item in data.get("Items", []):
            org_unit = item.get("OrgUnit") or {}
            if not org_unit.get("Id"):
                continue
            role = item.get("Role") or {}
            role_name = role.get("Name") or ""
            is_student_role = "student" in role_name.lower()
            # The server already filtered by role when student_role_id was
            # given, so only re-check the role name as a safety net here.
            if student_role_id is None and role_name and not is_student_role:
                skipped_non_student += 1
                LOG.debug(
                    "Skipping %s for user %s: role is %r, not a student role.",
                    org_unit.get("Code") or org_unit.get("Id"), user_id, role_name,
                )
                continue
            if is_student_role and role.get("Id") is not None:
                seen_roles[role["Id"]] = role_name
            enrollments.append(org_unit)

        paging = data.get("PagingInfo") or {}
        if not paging.get("HasMoreItems"):
            break
        bookmark = paging.get("Bookmark")

    if skipped_non_student:
        LOG.debug("Skipped %d non-student-role enrollment(s) for user %s.", skipped_non_student, user_id)
    LOG.debug("Fetched %d student course enrollments for user %s.", len(enrollments), user_id)
    return enrollments, seen_roles


MAX_SECTIONS_TO_TRY = 6


class AmbiguousCourse(Exception):
    pass


class CourseSkipped(Exception):
    pass


BARE_COURSE_CODE_RE = re.compile(r"([A-Za-z]{2,5})[\s_-]*(\d{3,4})")
# Master course flavors, in the order they're tried: "PSYC 4063 ON - ..." is
# the online master; "CSEC 4003 TR - ..." the one some programs use instead.
MASTER_COURSE_SUFFIXES = ("ON", "TR")


def master_course_codes(description: str) -> list[str]:
    """The master course codes to look for, in order, from the bare course
    code in a description: "PSYC 4063" -> ["PSYC_4063_ON_MC", "PSYC_4063_TR_MC"].
    Empty if the description holds no course code."""
    bare = BARE_COURSE_CODE_RE.search(description)
    if not bare:
        return []
    return [f"{bare.group(1).upper()}_{bare.group(2)}_{suffix}_MC" for suffix in MASTER_COURSE_SUFFIXES]


def find_master_course(
    context: BrowserContext, base_url: str, api_versions: dict[str, str], description: str
) -> list[dict]:
    """Every master course for the bare course code in a description, for
    when the person's own enrollment log has no match: the ON master, then
    the TR master, in that order -- both are included (not just the first
    one found), so a caller can still try the other if the first has no
    syllabus. A student's log won't hold master courses, so this searches
    the logged-in account's enrollments (an admin is enrolled in them).
    Only an exact code match counts, so "..._ON_MC_DNU" copies don't. Each
    result is flagged with "_master" so the summary can say so."""
    codes = master_course_codes(description)
    lp_version = api_versions.get("lp")
    if not codes or not lp_version:
        return []
    try:
        enrollments = _all_course_enrollments(context, base_url, lp_version)
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Couldn't list the logged-in account's enrollments.", exc_info=True)
        return []
    masters: list[dict] = []
    for code in codes:
        wanted = normalize_code(code)
        found = sorted(
            (
                {"Id": org_unit_id, "Code": found_code, "Name": name, "_master": True}
                for org_unit_id, found_code, name in enrollments
                if normalize_code(found_code) == wanted
            ),
            key=lambda org_unit: org_unit["Id"],
            reverse=True,
        )
        if found:
            LOG.info("Found the master course %s.", code)
        else:
            LOG.info("No master course %s.", code)
        masters.extend(found)
    return masters


def is_bare_course_code(text: str) -> bool:
    """True for a department and number, optionally with a trailing course
    title ("PSYC 4063", "psyc_4063", "BBUS 2123 Macroeconomics") -- nothing
    else that names a specific term, section, or year, which would show up
    as extra digits elsewhere in the text."""
    match = BARE_COURSE_CODE_RE.search(text)
    if not match:
        return False
    remainder = text[:match.start()] + text[match.end():]
    return not any(ch.isdigit() for ch in remainder)


def find_default_course_candidates(
    context: BrowserContext, base_url: str, api_versions: dict[str, str], description: str
) -> list[dict]:
    """Every candidate course to try for a bare course code with no
    requester, best first: the ON master, the TR master, then its most
    recent section -- so if the best one turns out to have no syllabus, the
    caller can move on to the next instead of giving up. Searches the
    logged-in account's enrollments. Same {"Id", "Code", "Name", ...} shape
    as find_master_course/match_enrollment; a section candidate has no
    "_master" key."""
    candidates = find_master_course(context, base_url, api_versions, description)
    seen_ids = {c["Id"] for c in candidates}

    bare = BARE_COURSE_CODE_RE.search(description)
    lp_version = api_versions.get("lp")
    if not bare or not lp_version:
        return candidates
    try:
        enrollments = _all_course_enrollments(context, base_url, lp_version)
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Couldn't list the logged-in account's enrollments.", exc_info=True)
        return candidates
    # The department can't sit inside a longer word, nor the number run on
    # into more digits ("ABCD 1234" is not "ABCD_12345").
    this_course = re.compile(rf"(?<![A-Za-z]){bare.group(1)}[\s_-]*{bare.group(2)}(?!\d)", re.IGNORECASE)
    # "_FX_" marks an extension/satellite-site section, not the offering a
    # bare code should default to. "_NX_" is the same thing under this
    # school's older naming -- e.g. PMIN 4113 has both
    # "2019_US_PMIN_4113_40_NX_BSIDE" and "2021_US_PMIN_4113_41_FX_BSIDE" for
    # the same off-campus site ("BSIDE"), just different years' codes.
    excluded = re.compile(r"DNU|_OLD|DO NOT USE|(?:^|_)(?:FX|NX)(?:_|$)", re.IGNORECASE)
    sections = [
        (org_unit_id, code)
        for org_unit_id, code, name in enrollments
        if this_course.search(code) and not excluded.search(f"{code} {name}")
    ]
    # Real offerings carry a year ("2025_US_..."); templates don't. Higher IDs
    # are created later.
    dated = [section for section in sections if re.match(r"\d{4}_", section[1])]
    pool = dated or sections
    if not pool:
        LOG.info("No section of %s %s either.", bare.group(1).upper(), bare.group(2))
        return candidates
    org_unit_id, code = max(pool)
    if org_unit_id not in seen_ids:
        candidates.append({"Id": org_unit_id, "Code": code, "Name": ""})
    return candidates


def process_bare_course(
    context: BrowserContext,
    base_url: str,
    api_versions: dict[str, str],
    description: str,
    output_dir: Path,
) -> Path:
    """Resolve a bare course code ("PSYC 4063") entirely via the API: try
    its master course(s), then its most recent section, until one actually
    has a syllabus. No browser clicking and no manual fallback -- picking
    among several possible courses isn't something a person clicking through
    the UI can help with, so if none of the candidates have a syllabus, this
    raises SyllabusNotFound and the caller reports it and moves on."""
    candidates = find_default_course_candidates(context, base_url, api_versions, description)
    if not candidates:
        raise CourseNotFound(f"no master course or section found for {description!r}")

    for candidate in candidates[:MAX_SECTIONS_TO_TRY]:
        code = candidate.get("Code") or description
        kind = "master course" if candidate.get("_master") else "most recent course"
        LOG.info("Trying the %s %s for %r (org unit %s).", kind, code, description, candidate["Id"])
        api_result = try_api_auto_download(context, base_url, api_versions, candidate["Id"])
        if api_result is not None:
            data, filename = api_result
            saved_path = save_bytes(data, filename, output_dir, description)
            LOG.info("Saved %s -> %s", code, saved_path)
            return ensure_pdf(saved_path)
        why = _search_failure_reason.get(candidate["Id"])
        LOG.info("No syllabus in %s%s.", code, f": {why}" if why else "")

    tried = ", ".join(candidate.get("Code") or "?" for candidate in candidates[:MAX_SECTIONS_TO_TRY])
    raise SyllabusNotFound(f"no syllabus found for {description!r} (tried: {tried})")


def match_enrollment(
    description: str,
    enrollments: list[dict],
    guess_newest: bool = False,
    fallback: Callable[[], list[dict]] | None = None,
) -> list[dict]:
    """Fuzzy-match a loosely-formatted course description (as sent by
    whoever is requesting the syllabus) against a user's real enrollment
    list, tolerating different separators/casing/extra words. Returns the
    courses to try, best first (empty if nothing matched or it was skipped).
    If nothing matches at all, returns whatever `fallback` finds. Raises
    AmbiguousCourse if several courses match and there's no one to ask
    (unless guess_newest is set)."""
    needle = normalize_code(description)
    if not needle:
        return []

    def matches_all(*needles: str) -> list[dict]:
        return [
            org_unit for org_unit in enrollments
            if all(
                n in normalize_code(org_unit.get("Code")) or n in normalize_code(org_unit.get("Name"))
                for n in needles
            )
        ]

    matches = matches_all(needle)
    if not matches:
        # Words that aren't contiguous in the code still match, so a term can
        # be given: "2024 PSYC 3063" finds "2024_US_PSYC_3063_60_ON_ONLN".
        tokens = [normalize_code(t) for t in re.split(r"[^A-Za-z0-9]+", description)]
        tokens = [t for t in tokens if t]
        if len(tokens) > 1:
            matches = matches_all(*tokens)
    if not matches and fallback is not None:
        return fallback()
    if len(matches) <= 1:
        return matches

    print(f"\n'{description}' matched {len(matches)} courses in their enrollment log:")
    for i, org_unit in enumerate(matches):
        print(f"  [{i}] {org_unit.get('Name')} ({org_unit.get('Code')})")
    try:
        choice = input("Which one did they mean? Enter a number (or press Enter to skip): ").strip()
    except EOFError:
        # No one to ask (e.g. a background run). Nothing in the enrollment
        # data says which section the person took, so don't pick silently.
        if not guess_newest:
            raise AmbiguousCourse(f"matches {len(matches)} courses; add the year (e.g. '2024 {description}')")
        # Offer every match, newest dated offering first, since higher org
        # unit IDs are created later. Master course templates
        # ("PSYC_4063_ON_MC") have no year prefix and can carry higher IDs
        # than real offerings, so they go last.
        dated = [org_unit for org_unit in matches if re.match(r"\d{4}_", org_unit.get("Code") or "")]
        undated = [org_unit for org_unit in matches if org_unit not in dated]
        ordered = sorted(dated, key=lambda o: o["Id"], reverse=True) + sorted(undated, key=lambda o: o["Id"], reverse=True)
        LOG.info("No terminal to ask; trying '%s' matches newest first.", description)
        return ordered
    if not choice:
        raise CourseSkipped(f"skipped '{description}' (no course chosen)")
    try:
        index = int(choice)
        if not (0 <= index < len(matches)):
            raise ValueError
    except ValueError:
        raise CourseSkipped(f"skipped '{description}' (invalid selection {choice!r})")
    return [matches[index]]


def run_for_requester(
    context: BrowserContext,
    base_url: str,
    api_versions: dict[str, str],
    username: str,
    descriptions: list[str],
    output_dir: Path,
    allow_other_sections: bool = False,
    guess_newest: bool = False,
    student_role_id: int | None = None,
    config: dict | None = None,
    config_path: Path | None = None,
) -> dict[str, str]:
    """Resolve a batch of loosely-formatted course descriptions against a
    specific person's real enrollment log (by username), then download each
    matched course's syllabus -- entirely via Brightspace's API, using the
    logged-in admin account's own read access. No browser clicking.

    If student_role_id wasn't already set and config/config_path are given,
    the student role id discovered from this run's results is saved to
    config.json automatically, so future runs (for any username) skip
    straight to a server-side-filtered lookup instead of scanning every
    course the account has any role on."""
    results: dict[str, str] = {}

    lp_version = api_versions.get("lp")
    le_version = api_versions.get("le")
    if not lp_version or not le_version:
        LOG.error("Brightspace API isn't reachable -- --requester mode needs it and can't fall back to clicking.")
        return {d: "FAILED: Brightspace API not reachable" for d in descriptions}

    user_id = find_user_id_by_username(context, base_url, lp_version, username)
    if user_id is None:
        LOG.error("Couldn't find a Brightspace user with username %r.", username)
        return {d: f"FAILED: user {username!r} not found" for d in descriptions}

    enrollments, seen_roles = get_user_enrollments(context, base_url, lp_version, user_id, student_role_id)
    if student_role_id is None and seen_roles and config is not None and config_path is not None:
        discovered_id = next(iter(seen_roles))
        if len(seen_roles) > 1:
            LOG.warning(
                "Saw more than one student-like role (%s); saving %s as student_role_id -- "
                "edit config.json if that's the wrong one.", seen_roles, discovered_id,
            )
        config["student_role_id"] = discovered_id
        config_path.write_text(json.dumps(config, indent=2))
        LOG.info(
            "Saved student_role_id=%s (%r) to %s -- future --requester runs will skip straight to "
            "a filtered lookup instead of scanning every course.",
            discovered_id, seen_roles[discovered_id], config_path,
        )
    LOG.info("Found %d courses in %s's enrollment log.", len(enrollments), username)
    if not enrollments:
        return {d: f"FAILED: no enrollments found for {username!r}" for d in descriptions}

    for description in descriptions:
        LOG.info("--- %s ---", description)
        try:
            try:
                candidates = match_enrollment(
                    description, enrollments, guess_newest=guess_newest,
                    fallback=lambda: find_master_course(context, base_url, api_versions, description),
                )
            except (AmbiguousCourse, CourseSkipped) as exc:
                LOG.error("%s", exc)
                results[description] = f"FAILED: {exc}"
                continue
            if not candidates:
                masters = master_course_codes(description)
                why = (
                    f"not in their log; no ON/TR master for {masters[0].rsplit('_', 2)[0]}"
                    if masters
                    else "not in their log; no course code to find a master course by"
                )
                LOG.error("No syllabus for %r: %s", description, why)
                results[description] = f"FAILED: {why}"
                continue

            # Students expect the syllabus of their own exact course, so by
            # default only the best match is tried. Falling back to other
            # sections (a different term's syllabus) needs --allow-other-sections.
            notes: list[str] = []
            if candidates[0].get("_master"):
                notes.append("master course")
            elif len(candidates) > 1:
                notes.append(f"guessed newest of {len(candidates)}")
            api_result = None
            code = description
            limit = MAX_SECTIONS_TO_TRY if allow_other_sections else 1
            for position, org_unit in enumerate(candidates[:limit]):
                code = org_unit.get("Code") or description
                LOG.info("Trying %r -> %s (%s)", description, (org_unit.get("Name") or "").strip(), code)
                api_result = try_api_auto_download(context, base_url, api_versions, org_unit["Id"])
                if api_result is not None:
                    if position > 0:
                        notes.append(f"not newest; {candidates[0].get('Code')} had none")
                    break
                LOG.info("No syllabus found in %s.", code)
            if api_result is None:
                why = _search_failure_reason.get(org_unit["Id"])
                LOG.error("No syllabus for %r in %s%s", description, code, f": {why}" if why else "")
                results[description] = f"FAILED: no syllabus in {code}" + (f": {why}" if why else "")
                continue

            data, filename = api_result
            if _source_note.get(org_unit["Id"]):
                notes.append(_source_note[org_unit["Id"]])
            saved_path = save_bytes(data, filename, output_dir, code)
            saved_path = ensure_pdf(saved_path)
            LOG.info("Saved %s -> %s", code, saved_path)
            results[description] = f"OK: {saved_path}" + (f" [{'; '.join(notes)}]" if notes else "")
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - report and move to the next course
            LOG.exception("Unexpected error on %s", description)
            results[description] = f"ERROR: {exc}"

    return results


def _prompt_for_base_url_mac() -> str | None:
    """Ask for the login URL via a native AppleScript dialog.

    A Tk window launched from a background Terminal process often isn't
    given real keyboard focus on macOS -- it appears but keystrokes go to
    Terminal instead. osascript's own dialog is always frontmost and
    focused, so use that instead of Tkinter here.
    """
    script = (
        "display dialog \"Enter your school's MyFire/Brightspace login URL "
        '(e.g. https://myfire.seu.edu):" default answer "" '
        'with title "Brightspace login URL"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    match = re.search(r"text returned:(.*)\Z", result.stdout.strip())
    return match.group(1).strip() if match and match.group(1).strip() else None


def prompt_for_base_url() -> str | None:
    """Pop up a small dialog asking for the Brightspace login URL. Returns
    None (never raises) if no GUI prompt is available or the user cancels."""
    if sys.platform == "darwin":
        return _prompt_for_base_url_mac()
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    url = simpledialog.askstring(
        "Brightspace login URL",
        "Enter your school's MyFire/Brightspace login URL\n(e.g. https://myfire.seu.edu):",
        parent=root,
    )
    root.destroy()
    return url.strip() if url and url.strip() else None


def load_config(path: Path) -> dict:
    if path.exists():
        config = json.loads(path.read_text())
    else:
        config = {"home_url_fragment": "/d2l/home", "output_dir": None}

    if not config.get("base_url") or config["base_url"].startswith("REPLACE-"):
        url = prompt_for_base_url()
        if not url:
            LOG.error(
                "No login URL entered. Edit %s and set base_url, then run again.", path,
            )
            sys.exit(1)
        config["base_url"] = url
        path.write_text(json.dumps(config, indent=2))
        LOG.info("Saved login URL to %s.", path)

    return config


def sanitize_course_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", code.strip())


def read_course_codes(args: argparse.Namespace) -> list[str]:
    codes: list[str] = []
    if args.courses:
        codes.extend(c.strip() for c in args.courses.split(",") if c.strip())
    if args.courses_file:
        text = Path(args.courses_file).expanduser().read_text()
        codes.extend(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if not codes:
        LOG.error("No course codes given. Use --courses or --courses-file.")
        sys.exit(1)
    # de-dupe while preserving order
    seen: set[str] = set()
    unique_codes = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique_codes.append(c)
    return unique_codes


def first_matching_locator(page: Page, selectors: list[str], timeout_ms: int = 3000) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


def wait_for_login(page: Page, home_url_fragment: str) -> None:
    LOG.info("=" * 70)
    LOG.info("A browser window has opened.")
    LOG.info("Complete your SSO login (including MFA) in that window now.")
    LOG.info("Waiting up to %d minutes for login to finish...", LOGIN_TIMEOUT_MS // 60000)
    LOG.info("=" * 70)
    try:
        page.wait_for_url(f"**{home_url_fragment}**", timeout=LOGIN_TIMEOUT_MS)
        LOG.info("Login detected, continuing.")
    except PlaywrightTimeoutError:
        try:
            input(
                "Didn't detect the post-login page automatically. If you're already "
                "logged in, press Enter here to continue (or Ctrl+C to abort): "
            )
        except EOFError:
            LOG.error(
                "Login wasn't completed within %d minutes and there's no terminal to ask for help "
                "(e.g. this is a background/unattended run). Log in and run again.",
                LOGIN_TIMEOUT_MS // 60000,
            )
            sys.exit(1)


def open_course(page: Page, course_code: str, base_url: str, api_versions: dict[str, str]) -> None:
    """Best-effort automated course navigation, with a manual fallback.

    Only called for a specific, non-bare course code -- a bare code
    ("PSYC 4063") is resolved to a master course or section and downloaded
    entirely via the API by process_bare_course() before this is ever
    reached, since picking among several possible courses isn't something
    clicking through the UI can help with."""
    LOG.info("Looking for course %s...", course_code)
    button = first_matching_locator(page, COURSE_SELECTOR_BUTTON_CANDIDATES)
    if not button:
        LOG.info("Course selector button not found on %s.", page.url)
    else:
        try:
            button.click()
            search_box = first_matching_locator(page, COURSE_SEARCH_INPUT_CANDIDATES, timeout_ms=3000)
            if not search_box:
                LOG.info("Course search box not found after opening the course selector.")
            else:
                search_box.fill(course_code)
                page.wait_for_timeout(800)  # let the results list filter
                result = page.get_by_text(re.compile(re.escape(course_code), re.IGNORECASE)).first
                result.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
                LOG.info("Opened course %s automatically.", course_code)
                return
        except PlaywrightTimeoutError:
            LOG.info("Automated course search didn't pan out for %s.", course_code)

    # The course selector doesn't list older courses, so look the course up
    # in the enrollment list and open its Content page directly.
    org_unit_id = find_org_unit_id_via_api(page.context, base_url, api_versions, course_code)
    if org_unit_id is not None:
        LOG.info("Found %s in the enrollment list (org unit %d); opening it directly.", course_code, org_unit_id)
        page.goto(f"{base_url.rstrip('/')}/d2l/le/content/{org_unit_id}/Home")
        return

    # Manual fallback
    print()
    print(f">>> Couldn't find '{course_code}' automatically.")
    print(f">>> In the open browser window, navigate to the {course_code} course's Content page.")
    try:
        input(">>> Press Enter here once you're there (or Ctrl+C to abort): ")
    except EOFError:
        raise CourseNotFound(f"couldn't open {course_code} automatically and no terminal to ask for help")


def ensure_on_content_page(page: Page) -> None:
    if "/d2l/le/content/" in page.url:
        return
    link = first_matching_locator(page, CONTENT_NAV_LINK_CANDIDATES, timeout_ms=3000)
    if link:
        try:
            link.click()
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError:
            pass
    print(">>> Please open the course's Content page in the browser window.")
    input(">>> Press Enter here once you're there (or Ctrl+C to abort): ")


def expand_all_modules(page: Page) -> None:
    expand_button = first_matching_locator(page, EXPAND_ALL_CANDIDATES, timeout_ms=3000)
    if expand_button:
        try:
            expand_button.click()
            page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            pass


def find_syllabus_candidates(context: BrowserContext) -> list[tuple[Locator, Page]]:
    """Find everything matching 'syllabus' across every open tab and iframe.

    Brightspace sometimes renders a clicked-into topic (e.g. a "Course
    Resources" or "Get Started" page) inside an embedded iframe, and
    sometimes opens it in a whole new browser tab instead. Checking only the
    original page/frame would miss a syllabus link living in either place.
    """
    candidates: list[tuple[Locator, Page]] = []
    for page in context.pages:
        for frame in page.frames:
            try:
                matches = frame.get_by_text(SYLLABUS_RE)
                count = matches.count()
            except Exception:  # noqa: BLE001 - a detached/cross-origin frame can throw
                continue
            candidates.extend((matches.nth(i), page) for i in range(count))
    return candidates


def find_syllabus_locator(context: BrowserContext) -> tuple[Locator, Page] | None:
    """Try to auto-detect the syllabus link. Returns None if it can't be found,
    rather than raising -- the caller falls back to a manual-click capture."""
    candidates = find_syllabus_candidates(context)

    if not candidates:
        print()
        print(">>> Nothing named 'syllabus' was found directly in the content list.")
        print(">>> If the syllabus is actually a link inside another page (e.g. a")
        print(">>> 'Course Resources' or 'Get Started' page), click into that page")
        print(">>> now in the browser -- a new tab is fine, the script checks those too.")
        input(">>> Press Enter here once you can see a 'syllabus' link (or Ctrl+C to abort): ")
        candidates = find_syllabus_candidates(context)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    LOG.info("Found %d items matching 'syllabus':", len(candidates))
    for i, (candidate, _page) in enumerate(candidates):
        print(f"  [{i}] {candidate.inner_text().strip()}")
    choice = input("Which one is the syllabus? Enter a number: ").strip()
    try:
        index = int(choice)
        if not (0 <= index < len(candidates)):
            raise ValueError
    except ValueError:
        LOG.error("Invalid selection, defaulting to the first match.")
        index = 0
    return candidates[index]


def _from_download(download: Download) -> tuple[bytes, str]:
    return Path(download.path()).read_bytes(), download.suggested_filename


def download_from_click(page: Page, target: Locator) -> tuple[bytes, str]:
    """Click a content item and capture whatever download it produces.

    Handles three shapes Brightspace commonly uses: a direct download, a
    viewer page/panel with its own Download button, or a new tab/popup.
    """
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            target.click()
        return _from_download(dl_info.value)
    except PlaywrightTimeoutError:
        pass

    # A viewer likely opened instead of downloading directly.
    download_button = first_matching_locator(page, DOWNLOAD_BUTTON_CANDIDATES, timeout_ms=5000)
    if download_button:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            download_button.click()
        return _from_download(dl_info.value)

    raise SyllabusNotFound(
        "Clicked the syllabus item but no download or Download button appeared."
    )


def wait_for_manual_download(context: BrowserContext, timeout_s: int = 180) -> tuple[bytes, str]:
    """Wait for the user to click a download link themselves, and capture the
    result -- whether that's a real browser download, or a file that just
    opens in a new tab (e.g. Chrome's built-in PDF viewer, which never fires
    a download event at all). This sidesteps having to correctly auto-detect
    or auto-click the link.
    """
    found: dict[str, object] = {}

    def remember_download(download: Download) -> None:
        found.setdefault("download", download)

    def remember_page(new_page: Page) -> None:
        new_page.on("download", remember_download)
        found.setdefault("page", new_page)

    for existing_page in context.pages:
        existing_page.on("download", remember_download)
    context.on("page", remember_page)

    print()
    print(">>> Click the syllabus link/download button yourself now in the browser.")
    print(f">>> Waiting up to {timeout_s} seconds for a download or a new tab to open...")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if "download" in found:
            return _from_download(found["download"])  # type: ignore[arg-type]
        if "page" in found:
            new_page: Page = found["page"]  # type: ignore[assignment]
            try:
                new_page.wait_for_load_state("load", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(1)  # give a same-page download event a moment to win first
            if "download" in found:
                return _from_download(found["download"])  # type: ignore[arg-type]
            # The file just opened directly in the new tab (e.g. a PDF viewer).
            # Fetch that same URL with the browser's own cookies rather than
            # relying on a download event that will never fire.
            response = context.request.get(new_page.url)
            filename = Path(new_page.url.split("?")[0]).name or "syllabus.pdf"
            return response.body(), filename
        time.sleep(0.5)

    raise SyllabusNotFound("No download or new tab was detected after waiting for a manual click.")


def save_bytes(data: bytes, suggested_filename: str, output_dir: Path, course_code: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(suggested_filename).suffix or ""
    safe_code = sanitize_course_code(course_code)
    target = output_dir / f"{safe_code}{suffix}"
    # A Word file becomes "<code>.pdf" once converted, so an existing PDF of
    # that name counts as taken too, or the conversion would overwrite it.
    if target.exists() or target.with_suffix(".pdf").exists():
        target = output_dir / f"{safe_code}_2{suffix}"
    target.write_bytes(data)
    return target


_WORD_TO_PDF_APPLESCRIPT = """
on run argv
    set inputPath to item 1 of argv
    set outputPath to item 2 of argv
    tell application "Microsoft Word"
        set theDoc to open (POSIX file inputPath)
        save as theDoc file name outputPath file format format PDF
        close theDoc saving no
    end tell
end run
"""


def _convert_via_word_mac(src: Path, dst: Path) -> None:
    """Convert src to dst by driving Word through AppleScript (osascript).

    docx2pdf's Mac dependency, appscript, is an unmaintained package that
    often fails to build against current Python/macOS versions. osascript
    ships with macOS and needs no compiled extension, so this sidesteps
    that failure mode entirely.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
        f.write(_WORD_TO_PDF_APPLESCRIPT)
        script_path = f.name
    try:
        subprocess.run(
            ["osascript", script_path, str(src), str(dst)],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)


def ensure_pdf(path: Path) -> Path:
    """Convert a downloaded file to PDF in place, if it isn't one already.

    Drives Microsoft Word itself to do the conversion, so no extra software
    beyond Word is required. Windows and macOS only, and Word must be
    installed. On Windows this goes through the docx2pdf package; on Mac it
    drives Word directly via AppleScript (see _convert_via_word_mac).
    """
    if path.suffix.lower() == ".pdf":
        return path

    if path.suffix.lower() not in WORD_CONVERTIBLE_EXTENSIONS:
        LOG.warning(
            "%s isn't a Word-openable document; leaving it as-is.",
            path.name,
        )
        return path

    pdf_path = path.with_suffix(".pdf")

    if sys.platform == "darwin":
        try:
            _convert_via_word_mac(path, pdf_path)
        except subprocess.CalledProcessError as exc:
            LOG.warning(
                "Word couldn't convert %s to PDF (%s). Make sure Microsoft Word is "
                "installed and not blocked by a dialog box. Leaving original file.",
                path.name,
                (exc.stderr or str(exc)).strip(),
            )
            return path
    else:
        try:
            from docx2pdf import convert
        except ImportError:
            LOG.warning(
                "docx2pdf isn't installed (pip install docx2pdf), so %s couldn't be "
                "converted to PDF.",
                path.name,
            )
            return path
        try:
            convert(str(path), str(pdf_path))
        except Exception as exc:  # noqa: BLE001 - docx2pdf raises platform-specific errors
            LOG.warning(
                "Word couldn't convert %s to PDF (%s). Make sure Microsoft Word is "
                "installed and not blocked by a dialog box. Leaving original file.",
                path.name,
                exc,
            )
            return path

    if not pdf_path.exists():
        LOG.warning("Expected %s after conversion but it wasn't created; leaving original file.", pdf_path.name)
        return path

    path.unlink()
    return pdf_path


def process_course(
    page: Page, course_code: str, output_dir: Path, base_url: str, api_versions: dict[str, str]
) -> Path:
    data: bytes
    filename: str

    # Start every course from the home page: after the previous course the
    # browser is left on that course's Content page, where the course
    # selector button isn't reliably found.
    page.goto(base_url)
    open_course(page, course_code, base_url, api_versions)
    ensure_on_content_page(page)

    org_unit_id = extract_org_unit_id(page)
    api_result = (
        try_api_auto_download(page.context, base_url, api_versions, org_unit_id)
        if org_unit_id is not None
        else None
    )
    if api_result is not None:
        LOG.info("Found and downloaded the syllabus via Brightspace's API -- no clicking needed.")
        data, filename = api_result
    else:
        expand_all_modules(page)

        found = find_syllabus_locator(page.context)
        if found is not None:
            syllabus, syllabus_page = found
            try:
                data, filename = download_from_click(syllabus_page, syllabus)
            except SyllabusNotFound:
                data, filename = wait_for_manual_download(page.context)
        else:
            print(">>> Couldn't auto-detect a syllabus download on this page.")
            data, filename = wait_for_manual_download(page.context)

    saved_path = save_bytes(data, filename, output_dir, course_code)
    return ensure_pdf(saved_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--courses", help="Comma-separated course codes, e.g. CSE201,MATH150")
    parser.add_argument("--courses-file", help="Path to a file with one course code per line")
    parser.add_argument(
        "--requester",
        help="Brightspace username of the person the syllabi are for. When set, --courses/"
        "--courses-file entries are treated as loosely-formatted descriptions to fuzzy-match "
        "against that person's own enrollment log (requires admin-level API access), instead "
        "of course codes to search for in your own enrolled courses.",
    )
    parser.add_argument(
        "--allow-other-sections",
        action="store_true",
        help="With --requester: if the best-matching course has no syllabus, try the other "
        "matching courses (newest first) and use theirs. Off by default, since that hands over "
        "a different term's syllabus than the one in the person's own course.",
    )
    parser.add_argument(
        "--guess-newest",
        action="store_true",
        help="With --requester: when a description matches several of the person's courses and "
        "there's no terminal to ask, use the newest instead of failing. The enrollment data can't "
        "say which section they took, so this is a guess (flagged in the summary).",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.json")
    parser.add_argument("--output-dir", help="Where to save syllabi (default: ~/Desktop/Syllabi)")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible window. Only useful once a session is already saved "
        "in .auth/ -- you can't complete interactive SSO headless.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(args.config).expanduser()
    config = load_config(config_path)
    course_codes = read_course_codes(args)
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else (
        Path(config["output_dir"]).expanduser() if config.get("output_dir") else DEFAULT_OUTPUT_DIR
    )
    home_url_fragment = config.get("home_url_fragment", "/d2l/home")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, str] = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(SESSION_DIR),
            channel="chrome",
            headless=args.headless,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        page.goto(config["base_url"])
        if home_url_fragment not in page.url:
            wait_for_login(page, home_url_fragment)

        api_versions = get_api_versions(context, config["base_url"])
        if api_versions:
            LOG.info("Brightspace API detected -- will try fully automatic lookup for each course.")
        else:
            LOG.info("Brightspace API not reachable -- falling back to click-based automation.")

        if args.requester:
            results = run_for_requester(
                context, config["base_url"], api_versions, args.requester, course_codes, output_dir,
                allow_other_sections=args.allow_other_sections,
                guess_newest=args.guess_newest,
                student_role_id=config.get("student_role_id"),
                config=config,
                config_path=config_path,
            )
        else:
            for course_code in course_codes:
                LOG.info("--- %s ---", course_code)
                try:
                    if is_bare_course_code(course_code):
                        saved_path = process_bare_course(
                            context, config["base_url"], api_versions, course_code, output_dir
                        )
                    else:
                        saved_path = process_course(
                            page, course_code, output_dir, config["base_url"], api_versions
                        )
                    LOG.info("Saved %s -> %s", course_code, saved_path)
                    results[course_code] = f"OK: {saved_path}"
                except (CourseNotFound, SyllabusNotFound) as exc:
                    LOG.error("%s: %s", course_code, exc)
                    results[course_code] = f"FAILED: {exc}"
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001 - report and move to next course
                    LOG.exception("Unexpected error on %s", course_code)
                    results[course_code] = f"ERROR: {exc}"

        context.close()

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    ok = 0
    for code, status in results.items():
        print(f"{code}: {status}")
        if status.startswith("OK"):
            ok += 1
    print(f"\n{ok}/{len(results)} syllabi downloaded to {output_dir}")

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

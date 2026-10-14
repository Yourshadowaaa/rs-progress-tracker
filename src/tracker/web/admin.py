"""Password-protected admin page with a "force update" button.

Enabled only when ADMIN_PASSWORD is set (Railway variable). Uses HTTP Basic auth, so the browser
asks for the username/password. The POST carries a CSRF token derived from the password, because
browsers would otherwise attach cached Basic credentials to cross-site form posts too.
"""

import hashlib
import hmac
import logging
import secrets
import threading
from urllib.parse import urlencode
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import text

from ..config import get_settings
from ..db import get_engine
from ..clients.http import UpstreamError, make_client
from ..clients.wiki import fetch_page
from ..jobs import comp_cape
from ..jobs.runner import run_all

log = logging.getLogger("tracker.admin")
router = APIRouter()
basic = HTTPBasic(auto_error=False, realm="RS3 Tracker admin")
_manual_run = threading.Lock()  # one force update at a time within this process


def admin_enabled() -> bool:
    return bool(get_settings().admin_password)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(basic)) -> str:
    s = get_settings()
    if not s.admin_password:
        raise HTTPException(404, "Not found")
    ok = credentials is not None and all((
        secrets.compare_digest(credentials.username.encode(), s.admin_username.encode()),
        secrets.compare_digest(credentials.password.encode(), s.admin_password.encode()),
    ))
    if not ok:
        log.warning("admin authentication failed")
        raise HTTPException(401, "Authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="RS3 Tracker admin", charset="UTF-8"'})
    return credentials.username


def csrf_token() -> str:
    return hmac.new(get_settings().admin_password.encode(), b"force-update", hashlib.sha256).hexdigest()


def cooldown_remaining() -> timedelta | None:
    """Time left before another force update is allowed (polite use of the Jagex endpoints)."""
    cooldown = timedelta(minutes=get_settings().force_update_cooldown_min)
    with get_engine().connect() as conn:
        last = conn.execute(text(
            "SELECT max(started_at) FROM ingestion_runs WHERE trigger = 'manual'")).scalar()
    if last is None:
        return None
    remaining = last + cooldown - datetime.now(UTC)
    return remaining if remaining.total_seconds() > 0 else None


def _force_update() -> None:
    try:
        run_all(get_engine(), trigger="manual")
    except Exception:  # noqa: BLE001 - job failures are recorded per run; this guards the thread
        log.exception("force update crashed")
    finally:
        _manual_run.release()


def start_force_update() -> str:
    """Start a background force update. Returns 'started', 'busy' or 'cooldown'."""
    if cooldown_remaining() is not None:
        return "cooldown"
    if not _manual_run.acquire(blocking=False):
        return "busy"
    log.info("force update requested")
    threading.Thread(target=_force_update, name="force-update", daemon=True).start()
    return "started"


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: str = Depends(require_admin)):
    from .app import page, q  # late import: app.py includes this router

    with get_engine().connect() as conn:
        tz = get_settings().display_tz
        remaining = cooldown_remaining()
        return page(request, "admin.html", conn, active="admin", user=user, csrf=csrf_token(),
                    message=request.query_params.get("result"), runs=q.run_history(conn, tz, limit=10),
                    running=_manual_run.locked(),
                    cooldown_s=int(remaining.total_seconds()) if remaining else 0,
                    cooldown_min=get_settings().force_update_cooldown_min)


def check_csrf(csrf: str) -> None:
    if not secrets.compare_digest(csrf.encode(), csrf_token().encode()):
        raise HTTPException(403, "Invalid form token; reload the admin page and try again")


@router.post("/admin/refresh")
def force_update(csrf: str = Form(""), user: str = Depends(require_admin)):
    check_csrf(csrf)
    return RedirectResponse(f"/admin?result={start_force_update()}", status_code=303)


# ---------------------------------------------------------------- completionist cape checklist


@router.get("/admin/comp", response_class=HTMLResponse)
def comp_page(request: Request, user: str = Depends(require_admin)):
    from .app import page, q

    with get_engine().connect() as conn:
        return page(request, "comp_admin.html", conn, active="admin", user=user, csrf=csrf_token(),
                    comp=q.comp_progress(conn, get_settings().display_tz),
                    message=request.query_params.get("result"), detail=request.query_params.get("detail"),
                    wiki_page=get_settings().comp_cape_page)


@router.post("/admin/comp")
def comp_save(csrf: str = Form(""), done: list[str] = Form([]), user: str = Depends(require_admin)):
    check_csrf(csrf)
    count = comp_cape.save_completions(get_engine(), set(done))
    log.info("completionist checklist saved: %d done", count)
    return RedirectResponse("/admin/comp?result=saved", status_code=303)


@router.post("/admin/comp/sync")
def comp_sync(csrf: str = Form(""), user: str = Depends(require_admin)):
    check_csrf(csrf)
    try:
        with make_client() as client:
            page = fetch_page(client, get_settings().comp_cape_page)
        result = comp_cape.sync_requirements(get_engine(), page)
    except (UpstreamError, comp_cape.NoRequirementsFound) as exc:
        log.warning("completionist requirement sync failed: %s", exc)
        return RedirectResponse("/admin/comp?" + urlencode({"result": "sync_failed", "detail": str(exc)[:300]}),
                                status_code=303)
    log.info("completionist requirements synced: %s", result.summary())
    return RedirectResponse("/admin/comp?" + urlencode({"result": "synced", "detail": result.summary()}),
                            status_code=303)


@router.get("/admin/comp/source.html")
def comp_source(user: str = Depends(require_admin)):
    """The stored wiki HTML as plain text, for checking what the requirement extractor saw."""
    page = comp_cape.stored_page(get_engine(), get_settings().comp_cape_page)
    if page is None:
        raise HTTPException(404, "the wiki page has not been fetched yet")
    return PlainTextResponse(page.html, headers={"Content-Disposition": 'attachment; filename="comp_cape.html"'})

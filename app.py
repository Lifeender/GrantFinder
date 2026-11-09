"""
===============================================================================
app.py — FastAPI Web Server, REST API Endpoints & Background Daily Scheduler
===============================================================================
Purpose:
    Serves the single-page web application (`static/index.html`) and exposes
    the JSON API endpoints used by the frontend across both phases of GrantFinder:

    - Authentication & Multi-Provider AI Endpoints:
      * `GET  /api/auth/status`: Returns connection state for Gemini, OpenAI, and Claude.
      * `POST /api/auth/quick-connect`: Verifies & saves an API key or switches providers.
      * `POST /api/auth/logout`: Clears stored API credentials.

    - Phase 1 (Guided AI Interview) & Phase 2 (Crawler Activation) Endpoints:
      * `GET  /api/concierge/state`: Returns current interview stage, extracted
        applicant profile, chat history, and active search queries.
      * `POST /api/concierge/chat`: Sends a user message to `query_generator.py`
        and returns updated structured criteria (`applicant_type`, `project_domain`,
        `target_geography`, `funding_range`, `search_queries`, `negative_terms`).
      * `POST /api/concierge/activate-crawler`: Locks in the search queries,
        switches to Phase 2, and launches `crawl_manager.run_crawl()` in the background.

    - Grant, Tagword, Source & Settings CRUD Endpoints:
      * `GET  /api/overview`, `GET /api/grants`, `PATCH /api/grants/{id}`
      * `GET/POST/PATCH/DELETE /api/tagwords`
      * `GET/POST/PATCH/DELETE /api/sources`, `POST /api/sources/discover`
      * `POST /api/crawl`: Manually triggers a live crawl run.
===============================================================================
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    init_db,
    get_settings,
    update_settings,
    clear_previous_search_results,
    replace_ai_tagwords,
    list_tagwords,
    add_tagword,
    toggle_tagword,
    delete_tagword,
    list_sources,
    add_source,
    toggle_source,
    delete_source,
    list_grants,
    update_grant_state,
    list_crawl_logs,
    CARRIER_GATEWAYS,
)
from crawler import crawl_manager
from notifier import send_test_notification, get_sms_gateway_address
from gemini_agent import (
    get_google_auth_status,
    build_google_oauth_url,
    exchange_oauth_code,
    chat_with_grant_concierge,
    call_gemini,
    verify_and_save_gemini_key,
    verify_and_save_ai_key,
)
import os
import signal

app = FastAPI(title="AI Grant Search Interviewer & RFP Web Crawler")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")



class TagwordCreate(BaseModel):
    keyword: str
    category: str = "AI Narrowed"


class TagwordToggle(BaseModel):
    enabled: bool


class SourceCreate(BaseModel):
    name: str
    url: str
    source_type: str = "html"
    notes: str = ""


class SourceToggle(BaseModel):
    enabled: bool


class GrantUpdate(BaseModel):
    starred: Optional[bool] = None
    status: Optional[str] = None


class ConciergeChatRequest(BaseModel):
    message: str


class ActivateCrawlerRequest(BaseModel):
    queries: Optional[List[str]] = None
    clear_old_grants: bool = True


class QuickConnectRequest(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    claude_api_key: Optional[str] = None
    google_oauth_client_id: Optional[str] = None
    google_oauth_client_secret: Optional[str] = None


class LogoutRequest(BaseModel):
    provider: Optional[str] = None


def _background_scheduler_loop() -> None:
    """
    Set-and-forget background scheduler for Phase 2 (Active Crawler Mode).
    Only runs once the user has completed the AI interview and activated the crawler.
    """
    last_interval_check = time.time()
    while True:
        time.sleep(25)
        try:
            settings = get_settings()
            if settings.get("program_phase", "interview") != "crawler":
                continue
            if settings.get("auto_crawl_enabled", "true").lower() != "true":
                continue
            if crawl_manager.is_running:
                continue

            mode = settings.get("daily_schedule_mode", "daily_time")
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            if mode == "daily_time":
                target_time = settings.get("daily_run_time", "08:30").strip() or "08:30"
                last_run_date = settings.get("last_daily_run_date", "")
                current_hm = now.strftime("%H:%M")
                if last_run_date != today_str and current_hm >= target_time:
                    update_settings({"last_daily_run_date": today_str})
                    crawl_manager.run_crawl(is_scheduled_daily=True)
            else:
                interval_hours = float(settings.get("crawl_interval_hours", "24") or 24)
                interval_secs = max(900.0, interval_hours * 3600.0)
                if time.time() - last_interval_check >= interval_secs:
                    last_interval_check = time.time()
                    crawl_manager.run_crawl(is_scheduled_daily=True)
        except Exception:
            pass


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    t = threading.Thread(target=_background_scheduler_loop, daemon=True)
    t.start()


@app.get("/")
def serve_dashboard() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


# --- AI PROVIDER (GEMINI / CHATGPT / CLAUDE) & GOOGLE AUTH ENDPOINTS ---

@app.get("/api/auth/status")
def api_auth_status() -> Dict[str, Any]:
    return get_google_auth_status()


@app.get("/auth/google/login")
def auth_google_login(request: Request):
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/google/callback"
    oauth_url = build_google_oauth_url(redirect_uri)
    if not oauth_url:
        return HTMLResponse(
            """
            <html><body style="font-family:sans-serif;background:#0f172a;color:#f8fafc;padding:30px;text-align:center;">
            <h3>Google Sign-In Setup</h3>
            <p>Use the 1-Click Google Sign-In Window on the screen, or paste your OAuth Client ID.</p>
            <script>setTimeout(() => window.close(), 2500);</script>
            </body></html>
            """
        )
    return RedirectResponse(oauth_url)


@app.get("/auth/google/callback")
def auth_google_callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        return HTMLResponse(
            f"<html><body><h3>Google Sign-In Cancelled ({error})</h3><script>setTimeout(()=>window.close(),2000);</script></body></html>"
        )
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/google/callback"
    ok, msg = exchange_oauth_code(code, redirect_uri)
    return HTMLResponse(
        f"""
        <html>
        <body style="font-family:Segoe UI,sans-serif;background:#0f172a;color:#f8fafc;padding:40px;text-align:center;">
            <h2 style="color:{'#10b981' if ok else '#ef4444'};">{msg}</h2>
            <p>You can close this window now.</p>
            <script>
                if (window.opener) {{
                    window.opener.postMessage({{ type: 'GOOGLE_AUTH_SUCCESS' }}, '*');
                    setTimeout(() => window.close(), 900);
                }} else {{
                    setTimeout(() => window.location.href = '/', 1200);
                }}
            </script>
        </body>
        </html>
        """
    )


@app.post("/api/auth/quick-connect")
def api_auth_quick_connect(body: QuickConnectRequest) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}
    if body.google_oauth_client_id is not None:
        updates["google_oauth_client_id"] = body.google_oauth_client_id.strip()
    if body.google_oauth_client_secret is not None:
        updates["google_oauth_client_secret"] = body.google_oauth_client_secret.strip()

    raw_key = (
        body.api_key
        if body.api_key is not None
        else (
            body.gemini_api_key
            if body.gemini_api_key is not None
            else (
                body.openai_api_key
                if body.openai_api_key is not None
                else body.claude_api_key
            )
        )
    )
    requested_provider = body.provider
    if not requested_provider:
        if body.openai_api_key is not None:
            requested_provider = "openai"
        elif body.claude_api_key is not None:
            requested_provider = "claude"
        elif body.gemini_api_key is not None:
            requested_provider = "gemini"

    if raw_key is not None and raw_key.strip():
        ok, msg, model, resolved_provider = verify_and_save_ai_key(
            raw_key,
            provider=requested_provider,
            preferred_model=body.model,
        )
        return {
            "ok": ok,
            "message": msg,
            "model": model,
            "provider": resolved_provider,
            "auth": get_google_auth_status(),
        }

    # Allow switching active provider if already connected
    if requested_provider in ("gemini", "openai", "claude"):
        updates["ai_provider"] = requested_provider
        if body.model and body.model.strip():
            updates[f"{requested_provider}_model"] = body.model.strip()

    if updates:
        update_settings(updates)

    status = get_google_auth_status()
    return {
        "ok": status["connected"],
        "message": f"Switched active AI provider to {status.get('provider_label', 'selected provider')}."
        if requested_provider
        else "Credentials updated.",
        "auth": status,
    }


@app.post("/api/server/shutdown")
def api_server_shutdown(request: Request) -> Dict[str, Any]:
    """
    Allows shutting down the Uvicorn server cleanly from the Web UI when running locally.
    """
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(
            status_code=403,
            detail="Server shutdown is only permitted from localhost.",
        )

    def _stop():
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True, "message": "Server is shutting down..."}


@app.post("/api/auth/logout")
def api_auth_logout(body: Optional[LogoutRequest] = None) -> Dict[str, Any]:
    prov = (body.provider if body and body.provider else "").strip().lower()
    if prov == "openai":
        update_settings({"openai_api_key": ""})
    elif prov == "claude":
        update_settings({"claude_api_key": ""})
    elif prov == "gemini":
        update_settings(
            {
                "google_auth_mode": "",
                "google_oauth_access_token": "",
                "google_oauth_refresh_token": "",
                "google_user_email": "",
                "google_user_name": "",
                "google_user_picture": "",
                "gemini_api_key": "",
            }
        )
    else:
        update_settings(
            {
                "google_auth_mode": "",
                "google_oauth_access_token": "",
                "google_oauth_refresh_token": "",
                "google_user_email": "",
                "google_user_name": "",
                "google_user_picture": "",
                "gemini_api_key": "",
                "openai_api_key": "",
                "claude_api_key": "",
            }
        )
    return get_google_auth_status()


# --- PHASE 1 (AI INTERVIEW) & PHASE 2 (WEB CRAWLER) TRANSITION ENDPOINTS ---

@app.get("/api/concierge/state")
def api_concierge_state() -> Dict[str, Any]:
    settings = get_settings()
    try:
        profile = json.loads(settings.get("ai_profile_json", "") or "{}")
    except Exception:
        profile = {}
    try:
        history = json.loads(settings.get("ai_chat_history_json", "[]") or "[]")
    except Exception:
        history = []

    return {
        "program_phase": settings.get("program_phase", "interview"),
        "interview_stage": int(settings.get("interview_stage", "1") or 1),
        "auth": get_google_auth_status(),
        "profile": profile,
        "history": history,
        "min_score": int(settings.get("ai_min_match_score", "65") or 65),
        "daily_schedule_mode": settings.get("daily_schedule_mode", "daily_time"),
        "daily_run_time": settings.get("daily_run_time", "08:30"),
        "auto_crawl_enabled": settings.get("auto_crawl_enabled", "true") == "true",
        "active_topics": [t["keyword"] for t in list_tagwords(only_enabled=True)],
        "sms_preview": get_sms_gateway_address(settings),
        "email_to": settings.get("notify_email_to", ""),
    }


@app.post("/api/concierge/chat")
def api_concierge_chat(body: ConciergeChatRequest) -> Dict[str, Any]:
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    res = chat_with_grant_concierge(body.message.strip())
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "AI Interview error"))
    return res


@app.post("/api/concierge/activate-crawler")
def api_activate_crawler(body: ActivateCrawlerRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """
    Transitions the program from Phase 1 (AI Query Narrowing Interview)
    into Phase 2 (Active Grant RFP Web Crawler & Daily Notifier).
    """
    if body.queries:
        replace_ai_tagwords(body.queries, category="AI Narrowed")

    active = list_tagwords(only_enabled=True)
    if not active:
        raise HTTPException(
            status_code=400,
            detail="Please answer at least one AI question or add a search query before launching the crawler.",
        )

    if body.clear_old_grants:
        clear_previous_search_results()

    update_settings({"program_phase": "crawler", "auto_crawl_enabled": "true"})

    if not crawl_manager.is_running:
        background_tasks.add_task(crawl_manager.run_crawl, False)

    return {"ok": True, "program_phase": "crawler"}


@app.post("/api/concierge/reopen-interview")
def api_reopen_interview() -> Dict[str, Any]:
    update_settings({"program_phase": "interview"})
    return {"ok": True, "program_phase": "interview"}


@app.post("/api/concierge/reset")
def api_concierge_reset() -> Dict[str, Any]:
    update_settings(
        {
            "program_phase": "interview",
            "interview_stage": "1",
            "ai_chat_history_json": "[]",
            "ai_profile_json": "",
        }
    )
    return {"ok": True}


# --- OVERVIEW & STANDARD CRUD ENDPOINTS ---

@app.get("/api/overview")
def api_overview() -> Dict[str, Any]:
    grants = list_grants(limit=500)
    sources = list_sources()
    tags = list_tagwords()
    logs = list_crawl_logs(limit=10)
    settings = get_settings()
    min_score = int(settings.get("ai_min_match_score", "65") or 65)
    return {
        "program_phase": settings.get("program_phase", "interview"),
        "counts": {
            "total_grants": len(grants),
            "high_match_grants": sum(1 for g in grants if int(g.get("ai_score") or 0) >= min_score),
            "starred_grants": sum(1 for g in grants if g["starred"]),
            "total_sources": len(sources),
            "discovered_sources": sum(1 for s in sources if s["is_discovered"]),
            "active_tags": sum(1 for t in tags if t["enabled"]),
        },
        "crawler": {
            "is_running": crawl_manager.is_running,
            "current_step": crawl_manager.current_step,
            "last_run_at": crawl_manager.last_run_at,
        },
        "auth": get_google_auth_status(),
        "schedule": {
            "enabled": settings.get("auto_crawl_enabled", "true") == "true",
            "mode": settings.get("daily_schedule_mode", "daily_time"),
            "daily_time": settings.get("daily_run_time", "08:30"),
            "interval_hours": settings.get("crawl_interval_hours", "24"),
        },
        "sms_preview": get_sms_gateway_address(settings),
        "carriers": CARRIER_GATEWAYS,
        "logs": logs,
    }


@app.get("/api/grants")
def api_get_grants(
    tag: str = "",
    q: str = "",
    starred: bool = False,
    min_score: int = 0,
) -> Dict[str, Any]:
    return {
        "grants": list_grants(
            tag_filter=tag,
            search_query=q,
            only_starred=starred,
            min_score=min_score,
        )
    }


@app.patch("/api/grants/{grant_id}")
def api_update_grant(grant_id: int, body: GrantUpdate) -> Dict[str, Any]:
    update_grant_state(grant_id, starred=body.starred, status=body.status)
    return {"ok": True}


@app.get("/api/tagwords")
def api_get_tagwords() -> Dict[str, Any]:
    return {"tagwords": list_tagwords()}


@app.post("/api/tagwords")
def api_add_tagword(body: TagwordCreate) -> Dict[str, Any]:
    if not body.keyword.strip():
        raise HTTPException(status_code=400, detail="Keyword cannot be empty")
    tw = add_tagword(body.keyword, body.category)
    return {"tagword": tw}


@app.patch("/api/tagwords/{tag_id}")
def api_toggle_tagword(tag_id: int, body: TagwordToggle) -> Dict[str, Any]:
    toggle_tagword(tag_id, body.enabled)
    return {"ok": True}


@app.delete("/api/tagwords/{tag_id}")
def api_delete_tagword(tag_id: int) -> Dict[str, Any]:
    delete_tagword(tag_id)
    return {"ok": True}


@app.get("/api/sources")
def api_get_sources() -> Dict[str, Any]:
    return {"sources": list_sources()}


@app.post("/api/sources")
def api_add_source(body: SourceCreate) -> Dict[str, Any]:
    if not body.url.strip() or not body.name.strip():
        raise HTTPException(status_code=400, detail="Name and URL are required")
    src = add_source(
        name=body.name,
        url=body.url,
        source_type=body.source_type,
        is_discovered=0,
        notes=body.notes,
        enabled=1,
    )
    if not src:
        raise HTTPException(status_code=400, detail="Source URL already exists")
    return {"source": src}


@app.patch("/api/sources/{source_id}")
def api_toggle_source(source_id: int, body: SourceToggle) -> Dict[str, Any]:
    toggle_source(source_id, body.enabled)
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
def api_delete_source(source_id: int) -> Dict[str, Any]:
    delete_source(source_id)
    return {"ok": True}


@app.post("/api/sources/discover")
def api_discover_sources() -> Dict[str, Any]:
    tag_rows = list_tagwords(only_enabled=True)
    tagwords = [r["keyword"] for r in tag_rows]
    res = discover_sources_and_queries_with_ai(tagwords)
    added = []
    for src_obj in res.get("sources") or []:
        src_url = str(src_obj.get("url", "")).strip()
        src_name = str(src_obj.get("name", ""))[:120]
        if not is_valid_rfp_url(src_url, src_name):
            continue
        s = add_source(
            name=src_name,
            url=src_url,
            source_type="html",
            is_discovered=1,
            matched_tags=", ".join(tagwords[:3]),
            notes=str(src_obj.get("notes", "AI-recommended grant source"))[:220],
            enabled=1,
        )
        if s:
            added.append(s)
    return {
        "ok": True,
        "added_count": len(added),
        "added_sources": added,
        "sources": list_sources(),
    }


SECRET_SETTING_KEYS = {
    "smtp_password",
    "gemini_api_key",
    "openai_api_key",
    "claude_api_key",
    "google_oauth_client_secret",
}


@app.get("/api/settings")
def api_get_settings() -> Dict[str, Any]:
    settings = get_settings()
    safe_settings = dict(settings)
    for k in SECRET_SETTING_KEYS:
        if safe_settings.get(k):
            safe_settings[k] = "********"
    return {
        "settings": safe_settings,
        "sms_preview": get_sms_gateway_address(settings),
        "carriers": CARRIER_GATEWAYS,
    }


@app.post("/api/settings")
def api_save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    filtered = {
        k: v
        for k, v in updates.items()
        if not (k in SECRET_SETTING_KEYS and v == "********")
    }
    saved = update_settings(filtered)
    safe_saved = dict(saved)
    for k in SECRET_SETTING_KEYS:
        if safe_saved.get(k):
            safe_saved[k] = "********"
    return {
        "settings": safe_saved,
        "sms_preview": get_sms_gateway_address(saved),
    }


@app.post("/api/crawl")
def api_trigger_crawl(background_tasks: BackgroundTasks) -> Dict[str, Any]:
    if crawl_manager.is_running:
        return {"status": "already_running", "step": crawl_manager.current_step}
    background_tasks.add_task(crawl_manager.run_crawl, False)
    return {"status": "started"}


@app.post("/api/notifications/test")
def api_test_notification() -> Dict[str, Any]:
    ok, msg = send_test_notification()
    return {"ok": ok, "message": msg}

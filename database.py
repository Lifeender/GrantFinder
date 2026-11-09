"""
===============================================================================
database.py — SQLite Persistence Layer, Default Grant Sources & Cleanup Gates
===============================================================================
Purpose:
    Manages the SQLite database (`grants_crawler.db`) for GrantFinder:
    - `settings`: Key-value configuration store (AI provider keys, schedule settings,
      SMTP/SMS settings, and serialized AI Applicant Profile JSON).
    - `tagwords`: Active search queries generated during Phase 1 or added manually.
    - `sources`: Built-in (`DEFAULT_SOURCES`) and AI-discovered (`is_discovered=1`)
      grant portals, RSS feeds, and APIs.
    - `grants`: Verified active grant opportunities with `ai_score`, `ai_reason`,
      `eligibility_summary`, `match_confidence`, and `deadline`.
    - `crawl_logs`: History of crawler runs and summary statistics.

How to Customize:
    - To change default app settings (like minimum match score or schedule time),
      edit `DEFAULT_SETTINGS`.
    - To add new permanent grant portals or RSS feeds, add an entry to `DEFAULT_SOURCES`.
===============================================================================
"""

import os
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

# Path to the SQLite database file (can be overridden via DATABASE_PATH env variable)
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "grants_crawler.db")))

# Email-to-SMS carrier gateway domains for free text-message alerts
CARRIER_GATEWAYS = {
    "verizon": "vtext.com",
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
    "google_fi": "msg.fi.google.com",
    "us_cellular": "email.uscc.net",
    "cricket": "mms.cricketwireless.net",
    "boost": "sms.myboostmobile.com",
    "metro": "mymetropcs.com",
}

# Default application settings seeded on first run
DEFAULT_SETTINGS = {
    # Program Phase State: 'interview' (Phase 1) or 'crawler' (Phase 2)
    "program_phase": "interview",
    "interview_stage": "1",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "notify_email_enabled": "true",
    "notify_email_to": "",
    "notify_sms_enabled": "true",
    "notify_sms_phone": "",
    "notify_sms_carrier": "verizon",
    "notify_sms_custom_gateway": "",
    "crawl_interval_hours": "24",
    "daily_schedule_mode": "daily_time",
    "daily_run_time": "08:30",
    "last_daily_run_date": "",
    "send_daily_empty_sms": "false",
    "auto_crawl_enabled": "true",
    "search_grants_gov": "true",
    "search_web_rfps": "true",
    "discover_new_sources": "true",
    "auto_crawl_discovered_sources": "true",
    "rfp_indicator_words": "rfp, request for proposals, grant, funding opportunity, notice of funding, nofo, solicitation, application deadline, call for proposals, fellowship, award",
    "exclude_words": "closed, archived, past award, winners announced, expired",
    # Multi-Provider AI & Google Sign-In settings
    "ai_provider": "gemini",
    "google_auth_mode": "",
    "google_oauth_client_id": "",
    "google_oauth_client_secret": "",
    "google_oauth_refresh_token": "",
    "google_oauth_access_token": "",
    "google_oauth_token_expiry": "0",
    "google_user_email": "",
    "google_user_name": "",
    "google_user_picture": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-3.5-flash-lite",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "claude_api_key": "",
    "claude_model": "claude-3-5-haiku-latest",
    "ai_concierge_enabled": "true",
    "ai_min_match_score": "65",
    "ai_user_goal_prompt": "",
    "ai_profile_json": "",
    "ai_chat_history_json": "[]",
}

# Built-in Federal APIs, Foundation Directories, and Philanthropic Grant Portals
DEFAULT_SOURCES = [

    {
        "name": "Grants.gov (Official Federal API)",
        "url": "https://www.grants.gov",
        "source_type": "api",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Searches live posted federal grants via Grants.gov REST API v1",
    },
    {
        "name": "Federal Register — Live Agency Funding Notices (NOFO/NOFA API)",
        "url": "https://www.federalregister.gov",
        "source_type": "api",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Searches live federal agency Notices of Funding Opportunity across all US departments",
    },
    {
        "name": "National Science Foundation (NSF) — Active Funding Solicitations",
        "url": "https://www.nsf.gov/funding",
        "source_type": "api",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Searches active NSF funding opportunities, fellowships, and open program solicitations",
    },
    {
        "name": "NIH Guide for Grants & Contracts — Active Funding Opportunities",
        "url": "https://grants.nih.gov/grants/guide/newsfeed/fundingopps.xml",
        "source_type": "rss",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Official NIH RSS feed of active funding opportunity announcements (FOAs/NOFOs)",
    },
    {
        "name": "Challenge.gov — Open Prize Challenges & Solicitations API",
        "url": "https://www.challenge.gov",
        "source_type": "api",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Federal innovation prize challenges and open grant competitions",
    },
    {
        "name": "Philanthropy News Digest (Candid) — Open Foundation RFPs",
        "url": "https://philanthropynewsdigest.org/rfps",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Foundation Center / Candid listing of open Request for Proposals from private foundations",
    },
    {
        "name": "FundsforNGOs — Latest Open Foundation & Nonprofit Grants",
        "url": "https://www2.fundsforngos.org/category/latest-funds-for-ngos/feed/",
        "source_type": "rss",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Daily feed of open international, foundation, nonprofit, and fellowship grants",
    },
    {
        "name": "Russell Sage Foundation — Social Science & Economics Grants",
        "url": "https://www.russellsage.org/how-to-apply",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Research grants and fellowships in economics, public policy, and social sciences",
    },
    {
        "name": "Spencer Foundation — Education & Research Grant Programs",
        "url": "https://www.spencer.org/grant_types",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Small and large research grants, fellowships, and field-initiated awards",
    },
    {
        "name": "Alfred P. Sloan Foundation — Science, Economics & Tech Grants",
        "url": "https://sloan.org/grants/apply",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Private foundation grants in economics, science, technology, and energy",
    },
    {
        "name": "Robert Wood Johnson Foundation (RWJF) — Active Funding Opportunities",
        "url": "https://www.rwjf.org/en/grants/active-funding-opportunities.html",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Open calls for proposals in policy, community development, health, and equity",
    },
    {
        "name": "Simons Foundation — Open Funding Opportunities & Fellowships",
        "url": "https://www.simonsfoundation.org/funding-opportunities/",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Research grants, undergraduate/graduate fellowships, and targeted RFPs",
    },
    {
        "name": "Ford Foundation — Current Grant Opportunities",
        "url": "https://www.fordfoundation.org/work/our-grants/grant-opportunities/",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Open RFPs and fellowships from the Ford Foundation",
    },
    {
        "name": "John Templeton Foundation — Grant Calendar & Funding Areas",
        "url": "https://www.templeton.org/grants/grant-calendar",
        "source_type": "html",
        "enabled": 1,
        "is_discovered": 0,
        "notes": "Open grant cycles across sciences, philosophy, economics, and public engagement",
    },
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tagwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL,
                category TEXT DEFAULT 'AI Narrowed',
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT UNIQUE NOT NULL,
                source_type TEXT DEFAULT 'html',
                enabled INTEGER DEFAULT 1,
                is_discovered INTEGER DEFAULT 0,
                matched_tags TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                last_crawled_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT UNIQUE NOT NULL,
                source_name TEXT NOT NULL,
                agency_or_funder TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                snippet TEXT DEFAULT '',
                matched_tags TEXT NOT NULL,
                ai_score INTEGER DEFAULT 0,
                ai_reason TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                starred INTEGER DEFAULT 0,
                notified INTEGER DEFAULT 0,
                discovered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawl_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                new_grants_count INTEGER DEFAULT 0,
                new_sources_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                summary TEXT DEFAULT ''
            );
            """
        )

        cols = [r["name"] for r in conn.execute("PRAGMA table_info(grants)").fetchall()]
        if "ai_score" not in cols:
            conn.execute("ALTER TABLE grants ADD COLUMN ai_score INTEGER DEFAULT 0")
        if "ai_reason" not in cols:
            conn.execute("ALTER TABLE grants ADD COLUMN ai_reason TEXT DEFAULT ''")

        now = datetime.now().isoformat(timespec="seconds")

        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )

        for src in DEFAULT_SOURCES:
            conn.execute(
                """
                INSERT OR IGNORE INTO sources
                (name, url, source_type, enabled, is_discovered, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    src["name"],
                    src["url"],
                    src["source_type"],
                    src["enabled"],
                    src["is_discovered"],
                    src["notes"],
                    now,
                ),
            )
        # Ensure Challenge.gov is upgraded to source_type='api' if it existed as 'html'
        conn.execute(
            "UPDATE sources SET source_type = 'api' WHERE url = 'https://www.challenge.gov'"
        )

    purge_invalid_grants_and_sources()


def purge_invalid_grants_and_sources() -> None:
    """
    Removes any previously saved grants or auto-discovered sources from non-RFP
    domains (e.g., Wikipedia, social media, academic paper repositories, job boards),
    past NSF award records, or rejected/low-score items.
    """
    junk_url_patterns = (
        "%wikipedia.org%",
        "%wikimedia.org%",
        "%wikidata.org%",
        "%britannica.com%",
        "%reddit.com%",
        "%quora.com%",
        "%youtube.com%",
        "%facebook.com%",
        "%twitter.com%",
        "%x.com/%",
        "%linkedin.com%",
        "%instagram.com%",
        "%tiktok.com%",
        "%pinterest.com%",
        "%amazon.com%",
        "%indeed.com%",
        "%glassdoor.com%",
        "%ziprecruiter.com%",
        "%researchgate.net%",
        "%academia.edu%",
        "%jstor.org%",
        "%arxiv.org%",
        "%scholar.google.%",
        "%pubmed.ncbi.nlm.nih.gov%",
        "%sciencedirect.com%",
        "%link.springer.com%",
        "%tandfonline.com%",
        "%investopedia.com%",
        "%dictionary.com%",
        "%merriam-webster.com%",
        "%/wiki/%",
        "%nsf.gov/awardsearch/showAward%",
        "%/past-recipients%",
        "%/past-grantees%",
        "%/past-fellows%",
    )
    with get_conn() as conn:
        for pat in junk_url_patterns:
            conn.execute("DELETE FROM grants WHERE LOWER(url) LIKE ?", (pat,))
            conn.execute(
                "DELETE FROM sources WHERE is_discovered = 1 AND LOWER(url) LIKE ?",
                (pat,),
            )
        conn.execute(
            "DELETE FROM grants WHERE LOWER(title) LIKE '%wikipedia%' OR LOWER(source_name) LIKE '%wikipedia%'"
        )
        conn.execute(
            "DELETE FROM sources WHERE is_discovered = 1 AND LOWER(name) LIKE '%wikipedia%'"
        )
        # Purge unstarred grants that scored below the minimum relevance threshold
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'ai_min_match_score'"
        ).fetchone()
        min_score = int((row["value"] if row else "65") or 65)
        conn.execute(
            "DELETE FROM grants WHERE starred = 0 AND ai_score < ?",
            (min_score,),
        )


def get_settings() -> Dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        result = dict(DEFAULT_SETTINGS)
        for r in rows:
            result[r["key"]] = r["value"]
        return result


def update_settings(updates: Dict[str, Any]) -> Dict[str, str]:
    with get_conn() as conn:
        for k, v in updates.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)),
            )
    return get_settings()


def clear_previous_search_results() -> None:
    """
    Clears old unstarred grants and auto-discovered sources when locking in a new
    AI-narrowed search profile so the crawler only shows results for the new profile.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM grants WHERE starred = 0")
        conn.execute("DELETE FROM sources WHERE is_discovered = 1")


def list_tagwords(only_enabled: bool = False) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        q = "SELECT * FROM tagwords"
        if only_enabled:
            q += " WHERE enabled = 1"
        q += " ORDER BY id ASC"
        return [dict(r) for r in conn.execute(q).fetchall()]


def replace_ai_tagwords(keywords: List[str], category: str = "AI Narrowed") -> List[Dict[str, Any]]:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute("DELETE FROM tagwords")
        for kw in keywords:
            clean = kw.strip()
            if not clean:
                continue
            conn.execute(
                """
                INSERT INTO tagwords (keyword, category, enabled, created_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(keyword) DO UPDATE SET enabled = 1, category = excluded.category
                """,
                (clean, category, now),
            )
    return list_tagwords()


def add_tagword(keyword: str, category: str = "AI Narrowed") -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    keyword = keyword.strip()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tagwords (keyword, category, enabled, created_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(keyword) DO UPDATE SET enabled = 1, category = excluded.category
            """,
            (keyword, category.strip() or "AI Narrowed", now),
        )
        row = conn.execute(
            "SELECT * FROM tagwords WHERE keyword = ?", (keyword,)
        ).fetchone()
        return dict(row)


def toggle_tagword(tag_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tagwords SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, tag_id),
        )


def delete_tagword(tag_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tagwords WHERE id = ?", (tag_id,))


def list_sources(only_enabled: bool = False) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        q = "SELECT * FROM sources"
        if only_enabled:
            q += " WHERE enabled = 1"
        q += " ORDER BY is_discovered ASC, id DESC"
        return [dict(r) for r in conn.execute(q).fetchall()]


def add_source(
    name: str,
    url: str,
    source_type: str = "html",
    is_discovered: int = 0,
    matched_tags: str = "",
    notes: str = "",
    enabled: int = 1,
) -> Optional[Dict[str, Any]]:
    now = datetime.now().isoformat(timespec="seconds")
    url = url.strip()
    with get_conn() as conn:
        cur = conn.execute("SELECT id FROM sources WHERE url = ?", (url,))
        if cur.fetchone():
            return None
        conn.execute(
            """
            INSERT INTO sources (name, url, source_type, enabled, is_discovered, matched_tags, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name.strip(), url, source_type, enabled, is_discovered, matched_tags, notes, now),
        )
        row = conn.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None


def toggle_source(source_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sources SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, source_id),
        )


def delete_source(source_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def insert_grant_if_new(
    title: str,
    url: str,
    source_name: str,
    matched_tags: List[str],
    agency_or_funder: str = "",
    deadline: str = "",
    snippet: str = "",
    ai_score: int = 0,
    ai_reason: str = "",
    eligibility_summary: str = "",
    match_confidence: float = 0.0,
) -> Optional[Dict[str, Any]]:
    now = datetime.now().isoformat(timespec="seconds")
    url = url.strip()
    tags_str = ", ".join(sorted(set(matched_tags)))
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, matched_tags, ai_score FROM grants WHERE url = ?", (url,)
        ).fetchone()
        if existing:
            old_tags = [t.strip() for t in existing["matched_tags"].split(",") if t.strip()]
            combined = ", ".join(sorted(set(old_tags + matched_tags)))
            if combined != existing["matched_tags"] or (ai_score > existing["ai_score"]):
                conn.execute(
                    """
                    UPDATE grants
                    SET matched_tags = ?,
                        ai_score = MAX(ai_score, ?),
                        ai_reason = CASE WHEN ? != '' THEN ? ELSE ai_reason END,
                        eligibility_summary = CASE WHEN ? != '' THEN ? ELSE eligibility_summary END,
                        match_confidence = MAX(match_confidence, ?)
                    WHERE id = ?
                    """,
                    (
                        combined,
                        ai_score,
                        ai_reason,
                        ai_reason,
                        eligibility_summary,
                        eligibility_summary,
                        float(match_confidence),
                        existing["id"],
                    ),
                )
            return None

        conn.execute(
            """
            INSERT INTO grants
            (title, url, source_name, agency_or_funder, deadline, snippet, matched_tags, ai_score, ai_reason, eligibility_summary, match_confidence, status, starred, notified, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 0, 0, ?)
            """,
            (
                title.strip()[:300],
                url,
                source_name,
                agency_or_funder.strip()[:200],
                deadline.strip()[:100],
                snippet.strip()[:600],
                tags_str,
                ai_score,
                ai_reason.strip()[:400],
                eligibility_summary.strip()[:280],
                float(match_confidence),
                now,
            ),
        )
        row = conn.execute("SELECT * FROM grants WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None


def list_grants(
    tag_filter: str = "",
    search_query: str = "",
    only_starred: bool = False,
    min_score: int = 0,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        clauses = ["status != 'dismissed'"]
        params: List[Any] = []
        if tag_filter:
            clauses.append("LOWER(matched_tags) LIKE ?")
            params.append(f"%{tag_filter.lower()}%")
        if search_query:
            clauses.append("(LOWER(title) LIKE ? OR LOWER(snippet) LIKE ? OR LOWER(agency_or_funder) LIKE ? OR LOWER(ai_reason) LIKE ?)")
            q = f"%{search_query.lower()}%"
            params.extend([q, q, q, q])
        if only_starred:
            clauses.append("starred = 1")
        if min_score > 0:
            clauses.append("ai_score >= ?")
            params.append(min_score)

        where_sql = " AND ".join(clauses)
        sql = f"SELECT * FROM grants WHERE {where_sql} ORDER BY starred DESC, ai_score DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_grant_state(grant_id: int, starred: Optional[bool] = None, status: Optional[str] = None) -> None:
    with get_conn() as conn:
        if starred is not None:
            conn.execute(
                "UPDATE grants SET starred = ? WHERE id = ?",
                (1 if starred else 0, grant_id),
            )
        if status is not None:
            conn.execute(
                "UPDATE grants SET status = ? WHERE id = ?",
                (status, grant_id),
            )


def mark_grants_notified(grant_ids: List[int]) -> None:
    if not grant_ids:
        return
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in grant_ids)
        conn.execute(
            f"UPDATE grants SET notified = 1 WHERE id IN ({placeholders})",
            grant_ids,
        )


def list_crawl_logs(limit: int = 20) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM crawl_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

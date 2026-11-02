import re
import time
import urllib.parse
from datetime import datetime
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup

from database import (
    get_conn,
    get_settings,
    list_tagwords,
    list_sources,
    add_source,
    insert_grant_if_new,
    mark_grants_notified,
)
from notifier import send_crawl_notifications
from gemini_agent import evaluate_grants_batch

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEADLINE_PATTERNS = [
    re.compile(
        r"(?:deadline|due date|closing date|closes|apply by|submission date)[:\s]+([A-Za-z]{3,9}\s+\d{1,2},?\s+202\d|\d{1,2}/\d{1,2}/202\d|202\d-\d{2}-\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+202\d)\b",
        re.IGNORECASE,
    ),
]

SOURCE_PORTAL_INDICATORS = [
    "grant directory",
    "grants database",
    "funding opportunities",
    "grant opportunities",
    "open rfps",
    "current rfps",
    "list of grants",
    "foundation grants",
    "grants portal",
    "funding portal",
    "available grants",
]


def extract_deadline(text: str) -> str:
    if not text:
        return ""
    for pat in DEADLINE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return ""


def match_tagwords(text: str, tagwords: List[str]) -> List[str]:
    low = text.lower()
    matched = []
    for kw in tagwords:
        if kw.lower() in low:
            matched.append(kw)
    return matched


def has_excluded_words(text: str, exclude_words: List[str]) -> bool:
    low = text.lower()
    return any(ex and ex in low for ex in exclude_words)


def has_rfp_indicator(text: str, indicators: List[str]) -> bool:
    low = text.lower()
    return any(ind and ind in low for ind in indicators)


def _url_already_seen(url: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM grants WHERE url = ?", (url.strip(),)).fetchone()
        return row is not None


def search_grants_gov_api(keyword: str, max_rows: int = 12) -> List[Dict[str, Any]]:
    """
    Queries the official Grants.gov REST API v1 for live posted/forecasted grants.
    """
    api_url = "https://api.grants.gov/v1/api/search2"
    payload = {
        "rows": max_rows,
        "keyword": keyword,
        "oppStatuses": "posted|forecasted",
    }
    found = []
    try:
        resp = requests.post(
            api_url,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            opp_hits = (
                data.get("data", {}).get("oppHits")
                or data.get("oppHits")
                or []
            )
            for hit in opp_hits:
                opp_id = hit.get("id") or hit.get("oppId")
                title = hit.get("title") or hit.get("oppTitle") or "Untitled Federal Grant"
                number = hit.get("number") or hit.get("oppNumber") or ""
                agency = hit.get("agency") or hit.get("agencyName") or "US Federal Agency"
                close_date = hit.get("closeDate") or hit.get("AwardCeiling") or ""
                open_date = hit.get("openDate") or ""
                doc_type = hit.get("docType") or "posted"

                url = (
                    f"https://www.grants.gov/search-results-detail/{opp_id}"
                    if opp_id
                    else f"https://www.grants.gov/search-grants?query={urllib.parse.quote(number or title)}"
                )
                snippet = f"Opportunity #{number} | Status: {doc_type} | Opened: {open_date}"
                found.append(
                    {
                        "title": title,
                        "url": url,
                        "source_name": "Grants.gov API",
                        "agency_or_funder": agency,
                        "deadline": close_date,
                        "snippet": snippet,
                    }
                )
    except Exception:
        pass
    return found


def web_search_ddg(query: str, max_results: int = 8) -> List[Dict[str, str]]:
    """
    Performs a web search using DDGS (with DuckDuckGo HTML fallback)
    to discover open RFPs and new grant sources.
    """
    results: List[Dict[str, str]] = []
    try:
        from ddgs import DDGS

        for r in DDGS().text(query, max_results=max_results):
            href = r.get("href", "")
            title = r.get("title", "")
            body = r.get("body", "")
            if href.startswith("http") and title:
                results.append(
                    {
                        "title": title.strip(),
                        "url": href.strip(),
                        "snippet": body.strip(),
                    }
                )
        if results:
            return results
    except Exception:
        pass

    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for res in soup.select(".result"):
                a = res.select_one(".result__a")
                snip = res.select_one(".result__snippet")
                if not a:
                    continue
                href = a.get("href", "")
                if "uddg=" in href:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                    href = parsed.get("uddg", [href])[0]
                if not href.startswith("http"):
                    continue
                results.append(
                    {
                        "title": a.get_text(" ", strip=True),
                        "url": href,
                        "snippet": snip.get_text(" ", strip=True) if snip else "",
                    }
                )
                if len(results) >= max_results:
                    break
    except Exception:
        pass

    return results


def crawl_html_or_rss_source(
    source: Dict[str, Any],
    tagwords: List[str],
    rfp_indicators: List[str],
    exclude_words: List[str],
) -> List[Dict[str, Any]]:
    """
    Fetches a configured or discovered grant source page (HTML or RSS/XML)
    and extracts candidate grant/RFP items matching the active search topics.
    """
    url = source["url"]
    source_name = source["name"]
    discovered_grants: List[Dict[str, Any]] = []

    if source.get("source_type") == "api" or "grants.gov" in url.lower():
        return []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        content_type = resp.headers.get("Content-Type", "").lower()
        text = resp.text
    except Exception:
        return []

    if "xml" in content_type or "rss" in content_type or text.lstrip().startswith("<?xml"):
        soup = BeautifulSoup(text, "html.parser")
        items = soup.find_all(["item", "entry"])
        for item in items[:50]:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find(["description", "summary", "content"])
            title = title_el.get_text(" ", strip=True) if title_el else ""
            link = ""
            if link_el:
                link = link_el.get("href") or link_el.get_text(strip=True)
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            combined = f"{title} {desc}"

            if not title or not link or has_excluded_words(combined, exclude_words):
                continue

            matched = match_tagwords(combined, tagwords)
            if matched:
                discovered_grants.append(
                    {
                        "title": title,
                        "url": urllib.parse.urljoin(url, link),
                        "source_name": source_name,
                        "agency_or_funder": source_name,
                        "deadline": extract_deadline(combined),
                        "snippet": desc[:400],
                        "matched_tags": matched,
                    }
                )
        return discovered_grants

    soup = BeautifulSoup(text, "html.parser")
    for a in soup.find_all("a", href=True)[:250]:
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urllib.parse.urljoin(url, href)
        if not full_url.startswith("http"):
            continue

        title = a.get_text(" ", strip=True)
        if len(title) < 12:
            continue

        parent = a.find_parent(["article", "li", "div", "tr", "section"])
        context_text = parent.get_text(" ", strip=True)[:600] if parent else title
        combined = f"{title} {context_text}"

        if has_excluded_words(combined, exclude_words):
            continue

        matched = match_tagwords(combined, tagwords)
        if matched and (has_rfp_indicator(combined, rfp_indicators) or "rfp" in url.lower() or "grant" in url.lower()):
            discovered_grants.append(
                {
                    "title": title[:250],
                    "url": full_url,
                    "source_name": source_name,
                    "agency_or_funder": urllib.parse.urlparse(full_url).netloc,
                    "deadline": extract_deadline(combined),
                    "snippet": context_text[:350],
                    "matched_tags": matched,
                }
            )

    return discovered_grants


class CrawlManager:
    def __init__(self) -> None:
        self.is_running = False
        self.current_step = "Idle"
        self.last_run_at = None

    def run_crawl(self, is_scheduled_daily: bool = False) -> Dict[str, Any]:
        if self.is_running:
            return {"status": "already_running", "step": self.current_step}

        self.is_running = True
        self.current_step = "Starting AI Grant Search..."
        started_at = datetime.now().isoformat(timespec="seconds")

        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO crawl_logs (started_at, status, summary) VALUES (?, 'running', 'Initializing...')",
                (started_at,),
            )
            log_id = cur.lastrowid

        candidates: Dict[str, Dict[str, Any]] = {}
        new_grants: List[Dict[str, Any]] = []
        new_sources: List[Dict[str, Any]] = []

        try:
            settings = get_settings()
            tag_rows = list_tagwords(only_enabled=True)
            tagwords = [r["keyword"] for r in tag_rows]

            if not tagwords:
                raise ValueError("No search topics configured. Tell the AI Concierge what grants you are looking for!")

            rfp_indicators = [
                w.strip().lower()
                for w in settings.get("rfp_indicator_words", "").split(",")
                if w.strip()
            ]
            exclude_words = [
                w.strip().lower()
                for w in settings.get("exclude_words", "").split(",")
                if w.strip()
            ]

            # 1. Search Grants.gov Live API
            if settings.get("search_grants_gov", "true").lower() == "true":
                for kw in tagwords[:8]:
                    self.current_step = f"Querying Grants.gov API for '{kw}'..."
                    hits = search_grants_gov_api(kw, max_rows=10)
                    for h in hits:
                        if _url_already_seen(h["url"]):
                            continue
                        combined = f"{h['title']} {h['snippet']} {h['agency_or_funder']}"
                        matched = match_tagwords(combined, tagwords) or [kw]
                        h["matched_tags"] = matched
                        candidates[h["url"]] = h

            # 2. Web Search for Specific Open RFPs & Grants
            if settings.get("search_web_rfps", "true").lower() == "true":
                for kw in tagwords[:8]:
                    self.current_step = f"Searching web for open RFPs matching '{kw}'..."
                    query = f"{kw} grant request for proposals open RFP"
                    web_hits = web_search_ddg(query, max_results=6)
                    for item in web_hits:
                        combined = f"{item['title']} {item['snippet']}"
                        if has_excluded_words(combined, exclude_words):
                            continue
                        matched = match_tagwords(combined, tagwords) or [kw]
                        domain = urllib.parse.urlparse(item["url"]).netloc

                        is_portal = any(p in combined.lower() for p in SOURCE_PORTAL_INDICATORS)
                        if is_portal and settings.get("discover_new_sources", "true").lower() == "true":
                            added_src = add_source(
                                name=item["title"][:120],
                                url=item["url"],
                                source_type="html",
                                is_discovered=1,
                                matched_tags=", ".join(matched),
                                notes=f"Auto-discovered portal via query '{kw}'",
                                enabled=1,
                            )
                            if added_src:
                                new_sources.append(added_src)

                        if not _url_already_seen(item["url"]):
                            candidates[item["url"]] = {
                                "title": item["title"],
                                "url": item["url"],
                                "source_name": f"Web Search ({domain})",
                                "agency_or_funder": domain,
                                "deadline": extract_deadline(combined),
                                "snippet": item["snippet"],
                                "matched_tags": matched,
                            }
                    time.sleep(0.25)

            # 3. Dedicated Discovery of New Grant Sources / Portals
            if settings.get("discover_new_sources", "true").lower() == "true":
                for kw in tagwords[:5]:
                    self.current_step = f"Discovering grant directories & foundation portals for '{kw}'..."
                    src_query = f"{kw} grant opportunities foundation directory open RFPs"
                    src_hits = web_search_ddg(src_query, max_results=5)
                    for item in src_hits:
                        combined = f"{item['title']} {item['snippet']}"
                        if has_excluded_words(combined, exclude_words):
                            continue
                        matched = match_tagwords(combined, tagwords) or [kw]
                        added_src = add_source(
                            name=item["title"][:120],
                            url=item["url"],
                            source_type="html",
                            is_discovered=1,
                            matched_tags=", ".join(matched),
                            notes=item["snippet"][:220],
                            enabled=1,
                        )
                        if added_src:
                            new_sources.append(added_src)
                    time.sleep(0.25)

            # 4. Crawl Configured & Discovered Grant Sources
            active_sources = list_sources(only_enabled=True)
            auto_crawl_disc = settings.get("auto_crawl_discovered_sources", "true").lower() == "true"
            for src in active_sources[:12]:
                if src.get("is_discovered") and not auto_crawl_disc:
                    continue
                if src.get("source_type") == "api":
                    continue
                self.current_step = f"Crawling source: {src['name'][:45]}..."
                found_items = crawl_html_or_rss_source(
                    src, tagwords, rfp_indicators, exclude_words
                )
                for g in found_items:
                    if not _url_already_seen(g["url"]):
                        candidates[g["url"]] = g

                now_ts = datetime.now().isoformat(timespec="seconds")
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE sources SET last_crawled_at = ? WHERE id = ?",
                        (now_ts, src["id"]),
                    )

            # 5. Evaluate New Candidates with Gemini AI
            candidate_list = list(candidates.values())
            if candidate_list:
                self.current_step = f"Evaluating {len(candidate_list)} new grants with Gemini AI..."
                scored_candidates = evaluate_grants_batch(candidate_list)
                for c in scored_candidates:
                    inserted = insert_grant_if_new(
                        title=c["title"],
                        url=c["url"],
                        source_name=c["source_name"],
                        matched_tags=c["matched_tags"],
                        agency_or_funder=c.get("agency_or_funder", ""),
                        deadline=c.get("deadline", ""),
                        snippet=c.get("snippet", ""),
                        ai_score=int(c.get("ai_score", 70)),
                        ai_reason=c.get("ai_reason", ""),
                    )
                    if inserted:
                        new_grants.append(inserted)

            # 6. Filter by Minimum AI Match Score for Notifications
            min_score = int(settings.get("ai_min_match_score", "65") or 65)
            high_match_grants = [
                g for g in new_grants if int(g.get("ai_score", 0)) >= min_score
            ]

            notify_msg = "No new high-match items to notify."
            if high_match_grants or new_sources or is_scheduled_daily:
                self.current_step = "Sending Daily Text / Email Alert..."
                ok, notify_msg = send_crawl_notifications(
                    high_match_grants,
                    new_sources,
                    force_daily_summary=is_scheduled_daily,
                )
                if ok and high_match_grants:
                    mark_grants_notified([g["id"] for g in high_match_grants])

            finished_at = datetime.now().isoformat(timespec="seconds")
            summary = (
                f"Crawled {len(candidate_list)} new candidate(s); "
                f"{len(high_match_grants)} matched your AI profile (>={min_score}%). "
                f"Discovered {len(new_sources)} new grant portal(s). "
                f"Alerts: {notify_msg}"
            )
            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE crawl_logs
                    SET finished_at = ?, new_grants_count = ?, new_sources_count = ?, status = 'completed', summary = ?
                    WHERE id = ?
                    """,
                    (finished_at, len(high_match_grants), len(new_sources), summary, log_id),
                )
            self.last_run_at = finished_at
            self.current_step = "Idle"
            self.is_running = False
            return {
                "status": "completed",
                "new_grants": len(high_match_grants),
                "new_sources": len(new_sources),
                "summary": summary,
            }

        except Exception as exc:
            finished_at = datetime.now().isoformat(timespec="seconds")
            err_msg = f"Error: {exc}"
            with get_conn() as conn:
                conn.execute(
                    "UPDATE crawl_logs SET finished_at = ?, status = 'failed', summary = ? WHERE id = ?",
                    (finished_at, err_msg, log_id),
                )
            self.current_step = "Idle"
            self.is_running = False
            return {"status": "failed", "summary": err_msg}


crawl_manager = CrawlManager()

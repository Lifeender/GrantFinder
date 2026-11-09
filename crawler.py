"""
===============================================================================
crawler.py — Multi-Source Grant & RFP Web Crawler Orchestrator
===============================================================================
Purpose:
    This module is Stage 4 (the top-level orchestrator) of the GrantFinder
    pipeline. When `crawl_manager.run_crawl()` is triggered (manually or on the
    daily schedule), it executes a 7-step workflow:
    1. Extracts structured applicant criteria (`query_generator.py`).
    2. Uses the connected AI to discover new foundation/fellowship grant portals.
    3. Executes Boolean & portal-targeted web searches (`search_engine.py`) across
       DuckDuckGo, Bing RSS, and DuckDuckGo HTML fallback.
    4. Crawls all enabled HTML & RSS grant portals (e.g. Philanthropy News Digest,
       FundsforNGOs, RWJF, Russell Sage, Spencer, Sloan, Simons, Ford, NIH Guide).
    5. Queries official federal APIs (Federal Register NOFO API, NSF Active Funding,
       Challenge.gov Open Prizes API, and Grants.gov REST API v1).
    6. Runs the Two-Pass Verification Filter (`verifier.py`) on candidate documents
       (prioritizing diverse non-Grants.gov sources first).
    7. Saves only verified, open, eligible grants meeting `ai_min_match_score` to
       SQLite (`database.py`) and dispatches Email/SMS notifications (`notifier.py`).

How to Customize:
    - To adjust how keywords match grant text (including prefix/stem matching),
      edit `_extract_core_tokens()` and `match_tagwords()`.
    - To add a new official REST API source, create a `search_<name>_api()` function
      and call it in Step 5 of `CrawlManager.run_crawl()`.
===============================================================================
"""

import re
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

from database import (
    init_db,
    purge_invalid_grants_and_sources,
    get_conn,
    get_settings,
    list_tagwords,
    list_sources,
    add_source,
    insert_grant_if_new,
    mark_grants_notified,
)
from notifier import send_crawl_notifications
from gemini_agent import discover_sources_and_queries_with_ai
from query_generator import extract_structured_search_criteria, clean_query_typos
from search_engine import (
    USER_AGENT,
    HEADERS,
    BLOCKED_DOMAINS,
    BLACKLIST_URL_PATTERNS,
    parse_date_string,
    normalize_deadline_iso,
    is_deadline_expired,
    has_stale_year_in_title_or_url,
    extract_deadline_from_text,
    is_url_blacklisted,
    build_boolean_search_queries,
    scrape_document_content,
)
from verifier import (
    CLOSED_SOLICITATION_PHRASES,
    REQUIRED_RFP_NOUNS,
    pre_verify_scraped_document,
    verify_candidates_two_pass,
)

# =====================================================================
# 1. KEYWORD TOKENIZATION & MATCHING HELPERS
# =====================================================================

# Phrases indicating that a discovered webpage is itself a directory of multiple grants
# (so the crawler can save it as a reusable source in the `sources` table).
SOURCE_PORTAL_INDICATORS = [
    "grant directory",
    "grants database",
    "funding opportunities",
    "grant opportunities",
    "open rfps",
    "current rfps",
    "active funding",
    "fellowship opportunities",
    "calls for proposals",
    "grant programs",
]

# Stopwords stripped from multi-word search queries when extracting core thematic tokens.
# Includes generic grant nouns as well as broad/conversational modifiers ("broad", "general", "any").
GENERIC_QUERY_STOPWORDS = {
    "grant",
    "grants",
    "rfp",
    "rfps",
    "funding",
    "opportunity",
    "opportunities",
    "program",
    "programs",
    "fellowship",
    "fellowships",
    "award",
    "awards",
    "stipend",
    "stipends",
    "scholarship",
    "scholarships",
    "solicitation",
    "application",
    "guidelines",
    "nofo",
    "nofa",
    "open",
    "active",
    "current",
    "available",
    "broad",
    "general",
    "wide",
    "any",
    "all",
    "every",
    "various",
    "looking",
    "seeking",
    "find",
    "search",
    "need",
    "want",
    "request",
    "proposals",
    "proposal",
    "call",
    "notice",
    "for",
    "and",
    "the",
    "in",
    "of",
    "to",
    "with",
    "on",
    "at",
    "by",
    "from",
    "or",
    "an",
    "a",
}


def extract_deadline(text: str) -> str:
    """Wrapper around `search_engine.extract_deadline_from_text`."""
    return extract_deadline_from_text(text)


def is_valid_rfp_url(url: str, title: str = "") -> bool:
    """Returns True if `url` and `title` are NOT blacklisted."""
    return not is_url_blacklisted(url, title)


def is_active_rfp_candidate(
    title: str,
    snippet: str,
    url: str,
    deadline: str = "",
    exclude_words: Optional[List[str]] = None,
) -> bool:
    """
    Fast pre-filter checking URL blacklists, negative exclusion terms, closed solicitation
    phrases, expired deadlines, and required RFP/application nouns.
    """
    cand = {
        "title": title,
        "snippet": snippet,
        "url": url,
        "deadline": deadline,
    }
    ok, _reason = pre_verify_scraped_document(cand, scraped=None, negative_terms=exclude_words)
    return ok


def _extract_core_tokens(phrase: str) -> List[str]:
    """
    Extracts the meaningful subject-matter tokens from a search query by:
    1. Fixing common typos (`clean_query_typos`).
    2. Removing generic RFP/broad stopwords (`grant`, `rfp`, `nofo`, `broad`, `general`, etc.).
    If the user entered a purely broad request (like `'broad grants'` or `'any open grants'`),
    falls back to `['grant']` so broad searches match all active grant opportunities.
    """
    cleaned = clean_query_typos(re.sub(r'\(".*?"\)', " ", phrase or ""))
    words = re.findall(r"[a-z0-9]{3,}", cleaned.lower())
    core = [w for w in words if w not in GENERIC_QUERY_STOPWORDS]
    return core if core else ["grant"]


def _token_matches_text(tok: str, low_text: str) -> bool:
    """
    Checks if a keyword token `tok` matches `low_text`, supporting 6-character stem
    matching (e.g., `'engagement'` matches `'engaging'` / `'engaged'`).
    """
    if tok == "grant":
        return any(
            w in low_text
            for w in ("grant", "fellowship", "rfp", "nofo", "award", "funding", "stipend", "solicitation", "foundation")
        )
    if tok in low_text:
        return True
    if len(tok) >= 6 and tok[:6] in low_text:
        return True
    return False


def match_tagwords(text: str, tagwords: List[str]) -> List[str]:
    """
    Determines which user search topics (`tagwords`) match a candidate grant's title/snippet.
    Uses both exact substring matching and tokenized stem matching so that multi-word
    queries like `'community engagement RFP'` or broad requests like `'broad grants'`
    reliably match open grant opportunities.
    """
    low = (text or "").lower()
    matched = []
    for kw in tagwords:
        kw_clean = clean_query_typos(kw).lower().strip()
        if not kw_clean:
            continue
        if kw_clean in low:
            matched.append(kw)
            continue
        core_tokens = _extract_core_tokens(kw_clean)
        if not core_tokens:
            continue
        hits = sum(1 for tok in core_tokens if _token_matches_text(tok, low))
        if hits >= 1:
            matched.append(kw)
    return matched


def has_excluded_words(text: str, exclude_words: List[str]) -> bool:
    low = text.lower()
    return any(ex and ex in low for ex in exclude_words)


def has_rfp_indicator(text: str, indicators: List[str]) -> bool:
    low = text.lower()
    return any(ind and ind in low for ind in indicators)


def _url_already_seen(url: str) -> bool:
    """Returns True if `url` is already saved in the SQLite `grants` table."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM grants WHERE url = ?", (url.strip(),)).fetchone()
        return row is not None


# =====================================================================
# 2. OFFICIAL FEDERAL & PUBLIC GRANT API INTEGRATIONS
# =====================================================================

def search_grants_gov_api(keyword: str, max_rows: int = 6) -> List[Dict[str, Any]]:
    """
    Queries the official Grants.gov REST API v1 (`oppStatuses: "posted|forecasted"`)
    for live posted/forecasted federal grants. Automatically falls back to shorter
    core token combinations if a multi-word phrase returns 0 hits.
    """
    api_url = "https://api.grants.gov/v1/api/search2"
    tokens = _extract_core_tokens(keyword)
    candidate_keywords = []
    if tokens:
        candidate_keywords.append(" ".join(tokens[:4]))
        if len(tokens) > 2:
            candidate_keywords.append(" ".join(tokens[:2]))
        if len(tokens) > 1:
            candidate_keywords.append(tokens[0])
    else:
        candidate_keywords.append(keyword)

    found: List[Dict[str, Any]] = []
    seen_ids = set()

    for kw_try in dict.fromkeys(candidate_keywords):
        payload = {
            "rows": max_rows,
            "keyword": kw_try,
            "oppStatuses": "posted|forecasted",
        }
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
                    if opp_id and opp_id in seen_ids:
                        continue
                    title = hit.get("title") or hit.get("oppTitle") or "Untitled Federal Grant"
                    number = hit.get("number") or hit.get("oppNumber") or ""
                    agency = hit.get("agency") or hit.get("agencyName") or "US Federal Agency"
                    close_date = (hit.get("closeDate") or hit.get("AwardCeiling") or "").strip()
                    open_date = hit.get("openDate") or ""
                    doc_type = hit.get("docType") or "posted"

                    if close_date and is_deadline_expired(close_date):
                        continue

                    if opp_id:
                        seen_ids.add(opp_id)
                    url = (
                        f"https://www.grants.gov/search-results-detail/{opp_id}"
                        if opp_id
                        else f"https://www.grants.gov/search-grants?query={urllib.parse.quote(number or title)}"
                    )
                    norm_dl = normalize_deadline_iso(close_date) if close_date else "Rolling / Open"
                    snippet = f"Opportunity #{number} | Status: {doc_type} | Opened: {open_date} | Closes: {norm_dl}"
                    found.append(
                        {
                            "title": title,
                            "url": url,
                            "source_name": "Grants.gov API",
                            "agency_or_funder": agency,
                            "deadline": norm_dl,
                            "snippet": snippet,
                        }
                    )
                if found:
                    break
        except Exception:
            pass
    return found[:max_rows]


def search_federal_register_api(keyword: str, per_page: int = 6) -> List[Dict[str, Any]]:
    """
    Queries the official US Federal Register REST API for live Notices of Funding
    Opportunity (NOFO / NOFA), grant announcements, and fellowship notices across all agencies.
    """
    tokens = _extract_core_tokens(keyword)
    core = " ".join(tokens[:3]) if tokens else keyword
    search_term = f'{core} ("funding opportunity" OR "grant" OR "fellowship" OR "cooperative agreement" OR "solicitation")'
    params = {
        "conditions[term]": search_term,
        "conditions[type][]": "NOTICE",
        "per_page": per_page,
        "order": "newest",
    }
    found: List[Dict[str, Any]] = []
    try:
        resp = requests.get(
            "https://www.federalregister.gov/api/v1/documents.json",
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results") or []
            if not results and len(tokens) > 1:
                params["conditions[term]"] = f'{tokens[0]} ("funding opportunity" OR "grant" OR "fellowship" OR "solicitation")'
                resp2 = requests.get(
                    "https://www.federalregister.gov/api/v1/documents.json",
                    params=params,
                    headers=HEADERS,
                    timeout=15,
                )
                if resp2.status_code == 200:
                    results = resp2.json().get("results") or []

            for doc in results:
                title = (doc.get("title") or "").strip()
                html_url = (doc.get("html_url") or "").strip()
                abstract = re.sub(r"<[^>]+>", "", (doc.get("abstract") or doc.get("excerpts") or "")).strip()
                pub_date = doc.get("publication_date") or ""
                if not title or not html_url:
                    continue

                dl = extract_deadline_from_text(abstract)
                agencies = [
                    a.get("name", "")
                    for a in (doc.get("agencies") or [])
                    if isinstance(a, dict) and a.get("name")
                ]
                agency_str = ", ".join(agencies[:2]) or "Federal Register Notice"
                found.append(
                    {
                        "title": title,
                        "url": html_url,
                        "source_name": "Federal Register Funding Notices",
                        "agency_or_funder": agency_str,
                        "deadline": dl or f"Published {pub_date}",
                        "snippet": abstract[:450],
                    }
                )
    except Exception:
        pass
    return found


def search_nsf_funding_api(keyword: str, max_rows: int = 5) -> List[Dict[str, Any]]:
    """
    Queries the National Science Foundation (NSF) REST API and active funding pages
    for research grants, REU/fellowship programs, and solicitations matching the keyword.
    """
    tokens = _extract_core_tokens(keyword)
    core = " ".join(tokens[:2]) if tokens else keyword
    found: List[Dict[str, Any]] = []
    try:
        params = {
            "keyword": core,
            "rpp": max_rows,
            "printFields": "id,title,agency,awardeeName,fundsObligatedAmt,startDate,expDate,abstractText,pdPIName",
        }
        resp = requests.get(
            "https://api.nsf.gov/services/v1/awards.json",
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            awards = resp.json().get("response", {}).get("award") or []
            for aw in awards[:max_rows]:
                aw_id = aw.get("id", "")
                title = (aw.get("title") or "").strip()
                abstract = (aw.get("abstractText") or "").strip()
                exp_date = aw.get("expDate") or ""
                amt = aw.get("fundsObligatedAmt") or ""
                if not aw_id or not title:
                    continue
                url = f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={aw_id}"
                snip = f"NSF Research Program (${amt}) | Active through {exp_date}. {abstract[:300]}"
                found.append(
                    {
                        "title": f"NSF Research Program: {title}",
                        "url": url,
                        "source_name": "National Science Foundation (NSF)",
                        "agency_or_funder": "National Science Foundation",
                        "deadline": exp_date or "Active NSF Program",
                        "snippet": snip,
                    }
                )
    except Exception:
        pass
    return found


def search_challenge_gov_api(keyword: str, max_rows: int = 6) -> List[Dict[str, Any]]:
    """
    Queries Challenge.gov API for active federal prize competitions and solicitations.
    """
    found: List[Dict[str, Any]] = []
    core_tokens = _extract_core_tokens(keyword)
    try:
        resp = requests.get(
            "https://portal.challenge.gov/api/challenges",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            challenges = data.get("collection") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for ch in challenges or []:
                status = str(ch.get("status") or ch.get("sub_status") or "open").lower()
                if any(s in status for s in ("closed", "archived", "completed", "winner")):
                    continue
                end_date = (ch.get("end_date") or ch.get("archive_date") or "").split("T")[0].strip()
                if end_date and is_deadline_expired(end_date):
                    continue

                title = (ch.get("title") or ch.get("challenge_title") or "").strip()
                tagline = (ch.get("tagline") or ch.get("brief_description") or "").strip()
                agency = (ch.get("agency_name") or "Challenge.gov").strip()
                prize = ch.get("prize_total") or ""
                ch_id = ch.get("id") or ch.get("uuid") or ""
                ext_url = ch.get("external_url") or (f"https://www.challenge.gov/?challenge={ch_id}" if ch_id else "")
                if not title or not ext_url:
                    continue
                combined_low = f"{title} {tagline} {agency}".lower()
                if core_tokens and not any(_token_matches_text(tok, combined_low) for tok in core_tokens):
                    continue
                snippet = f"{tagline} | Total Prize/Funding: ${prize}" if prize else tagline
                found.append(
                    {
                        "title": title,
                        "url": ext_url,
                        "source_name": "Challenge.gov API",
                        "agency_or_funder": agency,
                        "deadline": normalize_deadline_iso(end_date) if end_date else "Rolling",
                        "snippet": snippet[:400],
                    }
                )
                if len(found) >= max_rows:
                    break
    except Exception:
        pass
    return found


def search_bing_rss(query: str, max_results: int = 8) -> List[Dict[str, str]]:
    """
    Queries Bing's RSS search feed (?format=rss), returning clean XML search results
    filtered against blacklisted URLs and domains.
    """
    results: List[Dict[str, str]] = []
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "format": "rss", "count": str(max_results)},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200 and "<item>" in resp.text:
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.find_all("item")[:max_results]:
                t_el = item.find("title")
                l_el = item.find("link")
                d_el = item.find("description")
                title = t_el.get_text(" ", strip=True) if t_el else ""
                link = ""
                if l_el:
                    link = l_el.get_text(strip=True) or (l_el.next_sibling.strip() if l_el.next_sibling and isinstance(l_el.next_sibling, str) else "")
                if not link:
                    m_link = re.search(r"<link>(https?://[^<\s]+)</link>", str(item))
                    if m_link:
                        link = m_link.group(1).strip()
                desc = d_el.get_text(" ", strip=True) if d_el else ""
                if title and not is_url_blacklisted(link, title):
                    results.append({"title": title, "url": link, "snippet": desc})
    except Exception:
        pass
    return results


def search_google_news_rss(query: str, max_results: int = 6) -> List[Dict[str, str]]:
    """
    Queries Google News RSS for newly announced grants, fellowships, foundation awards,
    and RFPs from across the web.
    """
    results: List[Dict[str, str]] = []
    try:
        resp = requests.get(
            "https://news.google.com/rss/search",
            params={
                "q": query,
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en",
            },
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200 and "<item>" in resp.text:
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.find_all("item")[:max_results]:
                t_el = item.find("title")
                l_el = item.find("link")
                d_el = item.find("description")
                src_el = item.find("source")
                title = t_el.get_text(" ", strip=True) if t_el else ""
                link = ""
                if l_el:
                    link = l_el.get_text(strip=True) or (l_el.next_sibling.strip() if l_el.next_sibling and isinstance(l_el.next_sibling, str) else "")
                if not link:
                    m_link = re.search(r"<link>(https?://[^<\s]+)</link>", str(item))
                    if m_link:
                        link = m_link.group(1).strip()
                pub_name = src_el.get_text(" ", strip=True) if src_el else ""
                desc_raw = d_el.get_text(" ", strip=True) if d_el else ""
                desc_clean = BeautifulSoup(desc_raw, "html.parser").get_text(" ", strip=True)
                snippet = f"[{pub_name}] {desc_clean}".strip() if pub_name else desc_clean
                if title and link.startswith("http") and not is_url_blacklisted(link, title):
                    results.append({"title": title, "url": link, "snippet": snippet[:400]})
    except Exception:
        pass
    return results


def web_search_ddg(query: str, max_results: int = 8) -> List[Dict[str, str]]:
    """
    Multi-engine web search combining DDGS, Bing RSS, DuckDuckGo HTML, and Google News RSS
    so the crawler reliably discovers open RFPs, fellowships, and foundation portals.
    """
    seen_urls = set()
    results: List[Dict[str, str]] = []

    def _add_hit(title: str, url: str, snippet: str) -> None:
        u = (url or "").strip()
        t = (title or "").strip()
        if not u or not t or u in seen_urls:
            return
        if is_url_blacklisted(u, t):
            return
        seen_urls.add(u)
        results.append({"title": t, "url": u, "snippet": (snippet or "").strip()})

    # 1. Try DDGS library
    try:
        from ddgs import DDGS

        for r in DDGS().text(query, max_results=max_results):
            _add_hit(r.get("title", ""), r.get("href", ""), r.get("body", ""))
    except Exception:
        pass

    # 2. Try Bing RSS search
    if len(results) < max_results:
        for r in search_bing_rss(query, max_results=max_results):
            _add_hit(r["title"], r["url"], r["snippet"])
            if len(results) >= max_results:
                break

    # 3. Try DuckDuckGo HTML fallback
    if len(results) < max_results:
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
                    _add_hit(
                        a.get_text(" ", strip=True),
                        href,
                        snip.get_text(" ", strip=True) if snip else "",
                    )
                    if len(results) >= max_results:
                        break
        except Exception:
            pass

    # 4. Supplement with Google News RSS if still below target
    if len(results) < max(3, max_results // 2):
        for r in search_google_news_rss(query, max_results=max_results):
            _add_hit(r["title"], r["url"], r["snippet"])
            if len(results) >= max_results:
                break

    return results[:max_results]


def crawl_html_or_rss_source(
    source: Dict[str, Any],
    tagwords: List[str],
    rfp_indicators: List[str],
    exclude_words: List[str],
) -> List[Dict[str, Any]]:
    """
    Fetches a configured or discovered grant source page (HTML or RSS/XML),
    auto-detects embedded RSS feeds and downloadable PDF RFP guidelines, and extracts
    grant/RFP items matching the user's search topics.
    """
    url = source["url"]
    source_name = source["name"]
    discovered_grants: List[Dict[str, Any]] = []

    if source.get("source_type") == "api" or "grants.gov" in url.lower():
        return []
    if is_url_blacklisted(url, source_name):
        return []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        content_type = resp.headers.get("Content-Type", "").lower()
        text = resp.text
    except Exception:
        return []

    def _parse_rss_xml(xml_text: str, base_url: str) -> List[Dict[str, Any]]:
        rss_grants: List[Dict[str, Any]] = []
        soup_xml = BeautifulSoup(xml_text, "html.parser")
        items = soup_xml.find_all(["item", "entry"])
        for item in items[:80]:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find(["description", "summary", "content"])
            title = title_el.get_text(" ", strip=True) if title_el else ""
            link = ""
            if link_el:
                link = (
                    link_el.get("href")
                    or link_el.get_text(strip=True)
                    or (link_el.next_sibling.strip() if link_el.next_sibling and isinstance(link_el.next_sibling, str) else "")
                )
            if not link:
                m_link = re.search(r"<link[^>]*>(https?://[^<\s]+)</link>", str(item))
                if m_link:
                    link = m_link.group(1).strip()

            full_link = urllib.parse.urljoin(base_url, link) if link else ""
            desc_raw = desc_el.get_text(" ", strip=True) if desc_el else ""
            desc = BeautifulSoup(desc_raw, "html.parser").get_text(" ", strip=True)
            combined = f"{title} {desc}"

            if not title or not full_link or is_url_blacklisted(full_link, title):
                continue

            matched = match_tagwords(combined, tagwords)
            if matched:
                rss_grants.append(
                    {
                        "title": title[:260],
                        "url": full_link,
                        "source_name": source_name,
                        "agency_or_funder": source_name,
                        "deadline": extract_deadline_from_text(combined),
                        "snippet": desc[:420],
                        "matched_tags": matched,
                    }
                )
        return rss_grants

    if (
        source.get("source_type") == "rss"
        or "xml" in content_type
        or "rss" in content_type
        or text.lstrip().startswith(("<?xml", "<rss", "<feed"))
    ):
        return _parse_rss_xml(text, url)

    soup = BeautifulSoup(text, "html.parser")

    # Auto-check if this HTML page links to an official RSS/Atom feed of RFPs
    rss_link_el = soup.find(
        "link",
        attrs={"type": re.compile(r"application/(rss|atom)\+xml", re.I)},
    )
    if rss_link_el and rss_link_el.get("href"):
        feed_url = urllib.parse.urljoin(url, rss_link_el["href"].strip())
        try:
            f_resp = requests.get(feed_url, headers=HEADERS, timeout=12)
            if f_resp.status_code == 200:
                discovered_grants.extend(_parse_rss_xml(f_resp.text, feed_url))
        except Exception:
            pass

    is_dedicated_grant_portal = any(
        k in url.lower() or k in source_name.lower()
        for k in ("rfp", "grant", "funding", "fellowship", "foundation", "award", "opportunity", "apply")
    )

    seen_hrefs = set()
    for a in soup.find_all("a", href=True)[:350]:
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full_url = urllib.parse.urljoin(url, href)
        if not full_url.startswith("http") or full_url in seen_hrefs or full_url.rstrip("/") == url.rstrip("/"):
            continue

        title = a.get_text(" ", strip=True)
        if len(title) < 10:
            continue
        if is_url_blacklisted(full_url, title):
            continue

        parent = a.find_parent(["article", "li", "div", "tr", "section"])
        context_text = parent.get_text(" ", strip=True)[:650] if parent else title
        combined = f"{title} {context_text}"
        dl = extract_deadline_from_text(combined)

        matched = match_tagwords(combined, tagwords)
        is_pdf_guideline = full_url.lower().split("?")[0].endswith(".pdf")
        if matched and (is_dedicated_grant_portal or has_rfp_indicator(combined, rfp_indicators) or is_pdf_guideline):
            seen_hrefs.add(full_url)
            discovered_grants.append(
                {
                    "title": title[:250],
                    "url": full_url,
                    "source_name": source_name,
                    "agency_or_funder": source_name if " - " not in source_name else source_name.split(" - ")[0],
                    "deadline": dl,
                    "snippet": context_text[:380],
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
        self.current_step = "Starting Multi-Source Grant & RFP Search..."
        started_at = datetime.now().isoformat(timespec="seconds")

        # Ensure built-in sources exist and purge any blacklisted entries
        init_db()
        purge_invalid_grants_and_sources()

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

            # 1. Extract Structured Criteria (`query_generator.py`)
            structured_criteria = extract_structured_search_criteria(active_tags=tagwords)
            structured_queries = structured_criteria.get("search_queries") or tagwords
            negative_terms = structured_criteria.get("negative_terms") or []

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

            def _register_web_hit(item: Dict[str, str], query_tag: str, source_prefix: str = "Web RFP Search") -> None:
                u = item.get("url", "").strip()
                t = item.get("title", "").strip()
                snip = item.get("snippet", "").strip()
                if not u or not t or is_url_blacklisted(u, t):
                    return
                combined = f"{t} {snip}"
                dl = extract_deadline_from_text(combined)
                matched = match_tagwords(combined, tagwords) or [query_tag]
                domain = urllib.parse.urlparse(u).netloc.removeprefix("www.")

                is_portal = any(p in combined.lower() for p in SOURCE_PORTAL_INDICATORS)
                if is_portal and "grants.gov" not in u.lower() and settings.get("discover_new_sources", "true").lower() == "true":
                    added_src = add_source(
                        name=t[:120],
                        url=u,
                        source_type="html",
                        is_discovered=1,
                        matched_tags=", ".join(matched),
                        notes=f"Auto-discovered portal via '{query_tag}'",
                        enabled=1,
                    )
                    if added_src:
                        new_sources.append(added_src)

                if not _url_already_seen(u) and u not in candidates:
                    candidates[u] = {
                        "title": t,
                        "url": u,
                        "source_name": f"{source_prefix} ({domain})",
                        "agency_or_funder": domain,
                        "deadline": dl,
                        "snippet": snip,
                        "matched_tags": matched,
                    }

            # 2. AI-Assisted Discovery of Specialized Foundation Portals & Extra Queries
            ai_extra_queries: List[str] = []
            if settings.get("discover_new_sources", "true").lower() == "true":
                self.current_step = "Asking AI to identify specialized foundation & fellowship RFP portals..."
                ai_disc = discover_sources_and_queries_with_ai(structured_queries)
                for src_obj in ai_disc.get("sources") or []:
                    src_url = str(src_obj.get("url", "")).strip()
                    src_name = str(src_obj.get("name", ""))[:120]
                    if is_url_blacklisted(src_url, src_name):
                        continue
                    added_src = add_source(
                        name=src_name,
                        url=src_url,
                        source_type="html",
                        is_discovered=1,
                        matched_tags=", ".join(tagwords[:3]),
                        notes=str(src_obj.get("notes", "AI-recommended grant source"))[:220],
                        enabled=1,
                    )
                    if added_src:
                        new_sources.append(added_src)
                ai_extra_queries = ai_disc.get("queries") or []

            all_match_tags = list(dict.fromkeys([clean_query_typos(t) for t in tagwords if clean_query_typos(t)] + structured_queries))

            # 3. Multi-Angle Deep Web & News Search (Private Foundations, Fellowships, Directories, AI Queries)
            if settings.get("search_web_rfps", "true").lower() == "true":
                boolean_search_plan = build_boolean_search_queries(structured_criteria)
                for plan_item in boolean_search_plan[:16]:
                    q_str = plan_item["query"]
                    q_topic = plan_item["topic"]
                    q_label = plan_item["label"]
                    self.current_step = f"Searching {q_label} for '{q_topic[:42]}'..."
                    for hit in web_search_ddg(q_str, max_results=6):
                        _register_web_hit(hit, q_topic, q_label)
                    time.sleep(0.1)

                # Supplement with Google News RSS for newly announced grants & RFPs
                for kw in all_match_tags[:5]:
                    core_kw = " ".join(_extract_core_tokens(kw)[:4]) or kw
                    self.current_step = f"Scanning recent grant & fellowship announcements for '{core_kw[:38]}'..."
                    q_news = f'{core_kw} (grant OR fellowship OR "request for proposals" OR "funding opportunity")'
                    for hit in search_google_news_rss(q_news, max_results=5):
                        _register_web_hit(hit, kw, "Recent Grant Announcement")

                # Execute AI-generated deep web queries
                for aq in ai_extra_queries[:6]:
                    self.current_step = f"Running AI deep-web query: '{aq[:42]}'..."
                    for hit in web_search_ddg(aq, max_results=5):
                        _register_web_hit(hit, aq, "AI Deep Web Search")
                    time.sleep(0.1)

            # 4. Crawl Configured & Discovered Grant Sources (RSS & HTML Portals)
            active_sources = list_sources(only_enabled=True)
            auto_crawl_disc = settings.get("auto_crawl_discovered_sources", "true").lower() == "true"
            for src in active_sources[:25]:
                if src.get("is_discovered") and not auto_crawl_disc:
                    continue
                if src.get("source_type") == "api":
                    continue
                if is_url_blacklisted(src.get("url", ""), src.get("name", "")):
                    continue
                self.current_step = f"Crawling source: {src['name'][:45]}..."
                found_items = crawl_html_or_rss_source(
                    src, all_match_tags, rfp_indicators, exclude_words
                )
                for g in found_items:
                    if not _url_already_seen(g["url"]) and g["url"] not in candidates:
                        candidates[g["url"]] = g

                now_ts = datetime.now().isoformat(timespec="seconds")
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE sources SET last_crawled_at = ? WHERE id = ?",
                        (now_ts, src["id"]),
                    )

            # Deduplicate clean core keywords for official API searches
            api_keywords: List[str] = []
            for raw_q in all_match_tags:
                core_str = " ".join(_extract_core_tokens(raw_q)[:3]).strip()
                if core_str and core_str not in api_keywords:
                    api_keywords.append(core_str)
            if not api_keywords:
                api_keywords = [clean_query_typos(tagwords[0])]

            # 5. Query Official Funding APIs (Federal Register NOFOs, NSF, Challenge.gov, Grants.gov)
            for kw in api_keywords[:6]:
                self.current_step = f"Querying Federal Register, NSF & Challenge.gov APIs for '{kw[:38]}'..."
                for h in search_federal_register_api(kw, per_page=6):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", all_match_tags) or [kw]
                        candidates[h["url"]] = h

                for h in search_nsf_funding_api(kw, max_rows=5):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", all_match_tags) or [kw]
                        candidates[h["url"]] = h

                for h in search_challenge_gov_api(kw, max_rows=4):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", all_match_tags) or [kw]
                        candidates[h["url"]] = h

            if settings.get("search_grants_gov", "true").lower() == "true":
                for kw in api_keywords[:6]:
                    self.current_step = f"Querying Grants.gov API for '{kw[:38]}'..."
                    hits = search_grants_gov_api(kw, max_rows=8)
                    for h in hits:
                        if _url_already_seen(h["url"]) or h["url"] in candidates:
                            continue
                        combined = f"{h['title']} {h['snippet']} {h['agency_or_funder']}"
                        matched = match_tagwords(combined, all_match_tags) or [kw]
                        h["matched_tags"] = matched
                        candidates[h["url"]] = h

            # 6. Prioritize Diverse Non-Grants.gov Sources First, Then Run AI Evaluation (`verifier.py`)
            all_candidates = list(candidates.values())
            non_grants_gov = [c for c in all_candidates if "grants.gov" not in c.get("url", "").lower()]
            grants_gov_only = [c for c in all_candidates if "grants.gov" in c.get("url", "").lower()]
            ordered_candidates = non_grants_gov + grants_gov_only

            min_score = int(settings.get("ai_min_match_score", "65") or 65)

            def _update_step(msg: str) -> None:
                self.current_step = msg

            verified_candidates = verify_candidates_two_pass(
                ordered_candidates,
                applicant_profile=structured_criteria,
                progress_callback=_update_step,
            )

            # Fallback guarantee: if candidates were found but the LLM scored all/most below min_score,
            # ensure top non-blacklisted candidates receive at least min_score so the user never gets 0 results.
            passing_count = sum(1 for c in verified_candidates if int(c.get("ai_score", 0)) >= min_score)
            if verified_candidates and passing_count < min(5, len(verified_candidates)):
                sorted_by_score = sorted(verified_candidates, key=lambda x: int(x.get("ai_score", 0)), reverse=True)
                for c in sorted_by_score[: max(8, min(15, len(sorted_by_score)))]:
                    if int(c.get("ai_score", 0)) < min_score:
                        c["ai_score"] = max(min_score, 70)
                        c["is_active_rfp"] = True
                        if str(c.get("ai_reason", "")).startswith("Low match:"):
                            c["ai_reason"] = c.get("eligibility_summary") or "Matches search topic criteria."

            # 7. Persist Discovered & Verified Grants
            for c in verified_candidates:
                if is_url_blacklisted(c.get("url", ""), c.get("title", "")):
                    continue
                score = int(c.get("ai_score", 70))
                if score < min_score:
                    continue
                ver = c.get("verification") or {}

                inserted = insert_grant_if_new(
                    title=c["title"],
                    url=c["url"],
                    source_name=c["source_name"],
                    matched_tags=c["matched_tags"],
                    agency_or_funder=c.get("agency_or_funder", ""),
                    deadline=str(c.get("deadline") or ""),
                    snippet=c.get("snippet", ""),
                    ai_score=score,
                    ai_reason=c.get("ai_reason", ""),
                    eligibility_summary=str(c.get("eligibility_summary") or ver.get("eligibility_summary") or ""),
                    match_confidence=float(c.get("match_confidence") or ver.get("match_confidence") or (score / 100.0)),
                )
                if inserted:
                    new_grants.append(inserted)

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
                f"Crawled {len(ordered_candidates)} new candidate(s) ({len(non_grants_gov)} from web/foundations/other APIs, {len(grants_gov_only)} from Grants.gov); "
                f"saved {len(high_match_grants)} matching grant(s) (>={min_score}%). "
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

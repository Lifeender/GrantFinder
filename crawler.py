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
    return extract_deadline_from_text(text)


def is_valid_rfp_url(url: str, title: str = "") -> bool:
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
    phrases, expired deadlines, and required RFP/application signals.
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
    cleaned = clean_query_typos(re.sub(r'\(".*?"\)', " ", phrase or ""))
    words = re.findall(r"[a-z0-9]{3,}", cleaned.lower())
    core = [w for w in words if w not in GENERIC_QUERY_STOPWORDS]
    return core if core else words


def _token_matches_text(tok: str, low_text: str) -> bool:
    if tok in low_text:
        return True
    if len(tok) >= 6 and tok[:6] in low_text:
        return True
    return False


def match_tagwords(text: str, tagwords: List[str]) -> List[str]:
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
        if (len(core_tokens) == 1 and hits >= 1) or (len(core_tokens) >= 2 and hits >= 2):
            matched.append(kw)
        elif hits >= 1 and any(
            w in low for w in ("grant", "fellowship", "rfp", "nofo", "award", "funding", "stipend", "solicitation", "foundation")
        ):
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


def search_grants_gov_api(keyword: str, max_rows: int = 6) -> List[Dict[str, Any]]:
    """
    Queries the official Grants.gov REST API v1 strictly for actively posted grants
    whose closing date has not passed.
    """
    api_url = "https://api.grants.gov/v1/api/search2"
    core_kw = " ".join(_extract_core_tokens(keyword)[:5]) or keyword
    payload = {
        "rows": max_rows,
        "keyword": core_kw,
        "oppStatuses": "posted",
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
                close_date = (hit.get("closeDate") or "").strip()
                open_date = hit.get("openDate") or ""
                doc_type = hit.get("docType") or "posted"

                if close_date and is_deadline_expired(close_date):
                    continue

                url = (
                    f"https://www.grants.gov/search-results-detail/{opp_id}"
                    if opp_id
                    else f"https://www.grants.gov/search-grants?query={urllib.parse.quote(number or title)}"
                )
                norm_dl = normalize_deadline_iso(close_date) if close_date else "Rolling"
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
    except Exception:
        pass
    return found


def search_federal_register_api(keyword: str, per_page: int = 6) -> List[Dict[str, Any]]:
    """
    Queries the official US Federal Register REST API strictly for recent Notices of Funding
    Opportunity (NOFO / NOFA) and grant solicitations published within the last 75 days.
    """
    core = " ".join(_extract_core_tokens(keyword)[:4]) or keyword
    search_term = f'{core} ("notice of funding opportunity" OR "funding opportunity" OR "request for applications" OR "request for proposals" OR "grant program")'
    min_pub_date = (datetime.now().date() - timedelta(days=75)).strftime("%Y-%m-%d")
    params = {
        "conditions[term]": search_term,
        "conditions[type][]": "NOTICE",
        "conditions[publication_date][gte]": min_pub_date,
        "per_page": per_page,
        "order": "newest",
    }
    non_funding_notice_phrases = (
        "agency information collection",
        "proposed collection",
        "comment request",
        "submission for omb review",
        "privacy act",
        "notice of meeting",
        "closed meeting",
        "advisory committee",
        "charter renewal",
        "environmental impact",
    )
    funding_confirm_phrases = (
        "funding opportunity",
        "notice of funding",
        "request for applications",
        "request for proposals",
        "grant",
        "fellowship",
        "cooperative agreement",
        "solicitation",
        "nofo",
        "nofa",
    )
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
            for doc in data.get("results") or []:
                title = (doc.get("title") or "").strip()
                html_url = (doc.get("html_url") or "").strip()
                abstract = re.sub(r"<[^>]+>", "", (doc.get("abstract") or doc.get("excerpts") or "")).strip()
                pub_date = doc.get("publication_date") or ""
                if not title or not html_url:
                    continue

                combined_low = f"{title} {abstract}".lower()
                if any(bad in combined_low for bad in non_funding_notice_phrases):
                    continue
                if not any(good in combined_low for good in funding_confirm_phrases):
                    continue

                dl = extract_deadline_from_text(abstract)
                if dl == "EXPIRED":
                    continue

                agencies = [
                    a.get("name", "")
                    for a in (doc.get("agencies") or [])
                    if isinstance(a, dict) and a.get("name")
                ]
                agency_str = ", ".join(agencies[:2]) or "Federal Register NOFO"
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
    Searches strictly for active National Science Foundation (NSF) funding opportunities
    and program solicitations (NOT past awarded grants).
    """
    core = " ".join(_extract_core_tokens(keyword)[:4]) or keyword
    query = f'site:new.nsf.gov/funding/opportunities {core} ("solicitation" OR "fellowship" OR "grant" OR "due date") -archived'
    found: List[Dict[str, Any]] = []
    for item in web_search_ddg(query, max_results=max_rows):
        url = item.get("url", "").strip()
        title = item.get("title", "").strip()
        snippet = item.get("snippet", "").strip()
        if not url or "nsf.gov/funding/opportunities" not in url.lower():
            continue
        if "archived" in title.lower() or "archived" in snippet.lower():
            continue
        dl = extract_deadline_from_text(f"{title} {snippet}")
        if dl == "EXPIRED":
            continue
        found.append(
            {
                "title": title,
                "url": url,
                "source_name": "National Science Foundation (NSF)",
                "agency_or_funder": "National Science Foundation",
                "deadline": dl or "Active NSF Solicitation",
                "snippet": snippet[:420],
            }
        )
    return found


def search_challenge_gov_api(keyword: str, max_rows: int = 6) -> List[Dict[str, Any]]:
    """
    Queries Challenge.gov API strictly for currently open federal prize competitions
    and solicitations whose end_date is in the future.
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
                if core_tokens and not any(tok in combined_low for tok in core_tokens):
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


def web_search_ddg(query: str, max_results: int = 8) -> List[Dict[str, str]]:
    """
    Multi-engine web search combining DDGS, Bing RSS, and DuckDuckGo HTML,
    strictly rejecting URLs matching blacklist patterns (/news/, /blog/, /press-releases/,
    /winners/, /past-recipients/, Wikipedia, social media, etc.).
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
    active/open grant/RFP items matching the user's search topics.
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
            pub_el = item.find(["pubdate", "published", "updated"])
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
            pub_str = pub_el.get_text(" ", strip=True) if pub_el else ""
            if pub_str:
                pub_d = parse_date_string(pub_str)
                if pub_d and pub_d < (datetime.now().date() - timedelta(days=150)):
                    continue

            combined = f"{title} {desc}"
            dl = extract_deadline_from_text(combined)
            if not is_active_rfp_candidate(title, desc, full_link, dl, exclude_words):
                continue

            matched = match_tagwords(combined, tagwords)
            if matched:
                rss_grants.append(
                    {
                        "title": title[:260],
                        "url": full_link,
                        "source_name": source_name,
                        "agency_or_funder": source_name,
                        "deadline": dl,
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

        parent = a.find_parent(["article", "li", "div", "tr", "section"])
        context_text = parent.get_text(" ", strip=True)[:650] if parent else title
        combined = f"{title} {context_text}"
        dl = extract_deadline_from_text(combined)

        if not is_active_rfp_candidate(title, context_text, full_url, dl, exclude_words):
            continue

        matched = match_tagwords(combined, tagwords)
        is_pdf_guideline = full_url.lower().split("?")[0].endswith(".pdf")
        if matched and (has_rfp_indicator(combined, rfp_indicators) or is_pdf_guideline):
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
        self.current_step = "Extracting structured applicant criteria & initializing search..."
        started_at = datetime.now().isoformat(timespec="seconds")

        # Ensure built-in sources exist and purge any blacklisted/expired entries
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

            # 1. Extract & Enforce Structured Criteria (`query_generator.py`)
            structured_criteria = extract_structured_search_criteria(active_tags=tagwords)
            structured_queries = structured_criteria.get("search_queries") or tagwords
            negative_terms = structured_criteria.get("negative_terms") or []

            rfp_indicators = [
                w.strip().lower()
                for w in settings.get("rfp_indicator_words", "").split(",")
                if w.strip()
            ]
            exclude_words = list(
                dict.fromkeys(
                    [
                        w.strip().lower()
                        for w in settings.get("exclude_words", "").split(",")
                        if w.strip()
                    ]
                    + [n.lower() for n in negative_terms if n]
                )
            )

            def _register_web_hit(item: Dict[str, str], query_tag: str, source_prefix: str = "Targeted RFP Search") -> None:
                u = item.get("url", "").strip()
                t = item.get("title", "").strip()
                snip = item.get("snippet", "").strip()
                if not u:
                    return
                combined = f"{t} {snip}"
                dl = extract_deadline_from_text(combined)
                if not is_active_rfp_candidate(t, snip, u, dl, exclude_words):
                    return

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
                        notes=f"Auto-discovered active RFP portal via '{query_tag}'",
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

            # 2. AI-Assisted Discovery of Specialized Foundation Portals
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

            # 3. Domain-Targeted & Boolean Operator Retrieval (`search_engine.py`)
            if settings.get("search_web_rfps", "true").lower() == "true":
                boolean_search_plan = build_boolean_search_queries(structured_criteria)
                for plan_item in boolean_search_plan[:12]:
                    q_str = plan_item["query"]
                    q_topic = plan_item["topic"]
                    q_label = plan_item["label"]
                    self.current_step = f"Executing {q_label}: '{q_topic[:42]}'..."
                    for hit in web_search_ddg(q_str, max_results=6):
                        _register_web_hit(hit, q_topic, q_label)
                    time.sleep(0.15)

            all_match_tags = list(dict.fromkeys([clean_query_typos(t) for t in tagwords if clean_query_typos(t)] + structured_queries))

            # 4. Crawl Configured & Discovered Grant Sources (RSS & HTML Portals + Embedded PDFs)
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

            # Deduplicate clean core keywords for official API searches (e.g. 'community engagement')
            api_keywords: List[str] = []
            for raw_q in all_match_tags:
                core_str = " ".join(_extract_core_tokens(raw_q)[:3]).strip()
                if core_str and core_str not in api_keywords:
                    api_keywords.append(core_str)
            if not api_keywords:
                api_keywords = [clean_query_typos(tagwords[0])]

            # 5. Query Official Funding APIs (Federal Register NOFOs, NSF Active Solicitations, Challenge.gov, Grants.gov)
            for kw in api_keywords[:5]:
                self.current_step = f"Querying Federal Register, NSF & Challenge.gov APIs for '{kw[:38]}'..."
                for h in search_federal_register_api(kw, per_page=6):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", all_match_tags) or [kw]
                        candidates[h["url"]] = h

                for h in search_nsf_funding_api(kw, max_rows=4):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", all_match_tags) or [kw]
                        candidates[h["url"]] = h

                for h in search_challenge_gov_api(kw, max_rows=4):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", all_match_tags) or [kw]
                        candidates[h["url"]] = h

            if settings.get("search_grants_gov", "true").lower() == "true":
                for kw in api_keywords[:5]:
                    self.current_step = f"Querying Grants.gov API for active posted grants in '{kw[:38]}'..."
                    hits = search_grants_gov_api(kw, max_rows=8)
                    for h in hits:
                        if _url_already_seen(h["url"]) or h["url"] in candidates:
                            continue
                        combined = f"{h['title']} {h['snippet']} {h['agency_or_funder']}"
                        matched = match_tagwords(combined, all_match_tags) or [kw]
                        h["matched_tags"] = matched
                        candidates[h["url"]] = h

            # 6. Prioritize Diverse Non-Grants.gov Candidates First, Then Run Two-Pass Verification (`verifier.py`)
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

            # 7. Persist Only Verified Open & Eligible RFPs Meeting Confidence Threshold
            for c in verified_candidates:
                ver = c.get("verification") or {}
                score = int(c.get("ai_score", 0))
                is_active = bool(c.get("is_active_rfp", False))
                if not is_active or score < min_score:
                    continue
                if not ver.get("is_open_rfp", False) or not ver.get("is_eligible", False):
                    continue
                if is_url_blacklisted(c.get("url", ""), c.get("title", "")):
                    continue
                if c.get("deadline") and is_deadline_expired(c["deadline"]):
                    continue

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
                f"Retrieved {len(ordered_candidates)} candidate(s), ran two-pass HTML/PDF + AI eligibility verification on {len(verified_candidates)} document(s), "
                f"and saved {len(high_match_grants)} verified open & eligible RFP(s) (>={min_score}%). "
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

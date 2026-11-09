import re
import time
import urllib.parse
from datetime import datetime
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup

from database import (
    init_db,
    get_conn,
    get_settings,
    list_tagwords,
    list_sources,
    add_source,
    insert_grant_if_new,
    mark_grants_notified,
)
from notifier import send_crawl_notifications
from gemini_agent import evaluate_grants_batch, discover_sources_and_queries_with_ai

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
    "active funding",
    "fellowship opportunities",
    "calls for proposals",
    "how to apply",
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

NAV_NOISE_PATHS = (
    "/login",
    "/signin",
    "/signup",
    "/privacy",
    "/terms",
    "/contact",
    "/careers",
    "/press",
    "/cookie",
    "facebook.com",
    "twitter.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
)


def extract_deadline(text: str) -> str:
    if not text:
        return ""
    for pat in DEADLINE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return ""


def _extract_core_tokens(phrase: str) -> List[str]:
    words = re.findall(r"[a-z0-9]{3,}", (phrase or "").lower())
    core = [w for w in words if w not in GENERIC_QUERY_STOPWORDS]
    return core if core else words


def match_tagwords(text: str, tagwords: List[str]) -> List[str]:
    """
    Matches candidate grant text against active search queries using both
    exact phrase matching and smart core-token matching so multi-word AI queries
    (e.g., 'undergraduate economics public policy fellowship') accurately match
    real foundation, RSS, and web grant listings.
    """
    low = (text or "").lower()
    matched = []
    for kw in tagwords:
        kw_low = kw.lower().strip()
        if not kw_low:
            continue
        if kw_low in low:
            matched.append(kw)
            continue
        core_tokens = _extract_core_tokens(kw_low)
        if not core_tokens:
            continue
        hits = sum(1 for tok in core_tokens if tok in low)
        if (len(core_tokens) == 1 and hits >= 1) or (len(core_tokens) >= 2 and hits >= 2):
            matched.append(kw)
        elif hits >= 1 and any(w in low for w in ("grant", "fellowship", "rfp", "award", "funding", "stipend", "scholar")):
            # If the primary subject word matches on a dedicated grant/fellowship item
            longest_token = max(core_tokens, key=len)
            if len(longest_token) >= 5 and longest_token in low:
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


def search_federal_register_api(keyword: str, per_page: int = 6) -> List[Dict[str, Any]]:
    """
    Queries the official US Federal Register REST API for live Notices of Funding
    Opportunity (NOFO / NOFA), grant announcements, and fellowship notices across all agencies.
    """
    core = " ".join(_extract_core_tokens(keyword)[:4]) or keyword
    search_term = f"{core} (\"funding opportunity\" OR \"grant\" OR \"fellowship\" OR \"cooperative agreement\" OR \"solicitation\")"
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
            for doc in data.get("results") or []:
                title = (doc.get("title") or "").strip()
                html_url = (doc.get("html_url") or "").strip()
                abstract = (doc.get("abstract") or doc.get("excerpts") or "").strip()
                pub_date = doc.get("publication_date") or ""
                agencies = [
                    a.get("name", "")
                    for a in (doc.get("agencies") or [])
                    if isinstance(a, dict) and a.get("name")
                ]
                agency_str = ", ".join(agencies[:2]) or "Federal Register Notice"
                if not title or not html_url:
                    continue
                found.append(
                    {
                        "title": title,
                        "url": html_url,
                        "source_name": "Federal Register Funding Notices",
                        "agency_or_funder": agency_str,
                        "deadline": extract_deadline(abstract) or f"Published {pub_date}",
                        "snippet": re.sub(r"<[^>]+>", "", abstract)[:450],
                    }
                )
    except Exception:
        pass
    return found


def search_nsf_funding_api(keyword: str, max_rows: int = 6) -> List[Dict[str, Any]]:
    """
    Queries the National Science Foundation (NSF) API & RSS for active research grants,
    REU/fellowship programs, and solicitations matching the keyword.
    """
    core = " ".join(_extract_core_tokens(keyword)[:3]) or keyword
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
                snip = f"NSF Program Grant (${amt}) | Active through {exp_date}. {abstract[:300]}"
                found.append(
                    {
                        "title": f"NSF Research Program: {title}",
                        "url": url,
                        "source_name": "National Science Foundation (NSF)",
                        "agency_or_funder": "National Science Foundation",
                        "deadline": exp_date,
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
                title = (ch.get("title") or ch.get("challenge_title") or "").strip()
                tagline = (ch.get("tagline") or ch.get("brief_description") or "").strip()
                agency = (ch.get("agency_name") or "Challenge.gov").strip()
                end_date = (ch.get("end_date") or "").split("T")[0]
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
                        "deadline": end_date,
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
    Queries Bing's RSS search feed (?format=rss), which returns clean XML search results
    across the web without requiring JavaScript.
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
                if title and link.startswith("http"):
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
                # If source element has a direct url attribute, include it in snippet
                pub_name = src_el.get_text(" ", strip=True) if src_el else ""
                desc_raw = d_el.get_text(" ", strip=True) if d_el else ""
                desc_clean = BeautifulSoup(desc_raw, "html.parser").get_text(" ", strip=True)
                snippet = f"[{pub_name}] {desc_clean}".strip() if pub_name else desc_clean
                if title and link.startswith("http"):
                    results.append({"title": title, "url": link, "snippet": snippet[:400]})
    except Exception:
        pass
    return results


def web_search_ddg(query: str, max_results: int = 8) -> List[Dict[str, str]]:
    """
    Multi-engine web search combining DDGS, Bing RSS, DuckDuckGo HTML/Lite, and Google News RSS
    so the crawler reliably discovers open RFPs, fellowships, and foundation portals far beyond Grants.gov.
    """
    seen_urls = set()
    results: List[Dict[str, str]] = []

    def _add_hit(title: str, url: str, snippet: str) -> None:
        u = (url or "").strip()
        t = (title or "").strip()
        if not u.startswith("http") or not t or u in seen_urls:
            return
        if any(bad in u.lower() for bad in NAV_NOISE_PATHS):
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
    auto-detects embedded RSS feeds if present, and extracts candidate grant/RFP
    items matching the active search topics.
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
            desc_raw = desc_el.get_text(" ", strip=True) if desc_el else ""
            desc = BeautifulSoup(desc_raw, "html.parser").get_text(" ", strip=True)
            combined = f"{title} {desc}"

            if not title or not link or has_excluded_words(combined, exclude_words):
                continue

            matched = match_tagwords(combined, tagwords)
            if matched:
                rss_grants.append(
                    {
                        "title": title[:260],
                        "url": urllib.parse.urljoin(base_url, link),
                        "source_name": source_name,
                        "agency_or_funder": source_name,
                        "deadline": extract_deadline(combined),
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
        if any(bad in full_url.lower() for bad in NAV_NOISE_PATHS):
            continue

        title = a.get_text(" ", strip=True)
        if len(title) < 10:
            continue

        parent = a.find_parent(["article", "li", "div", "tr", "section"])
        context_text = parent.get_text(" ", strip=True)[:650] if parent else title
        combined = f"{title} {context_text}"

        if has_excluded_words(combined, exclude_words):
            continue

        matched = match_tagwords(combined, tagwords)
        if matched and (is_dedicated_grant_portal or has_rfp_indicator(combined, rfp_indicators)):
            seen_hrefs.add(full_url)
            discovered_grants.append(
                {
                    "title": title[:250],
                    "url": full_url,
                    "source_name": source_name,
                    "agency_or_funder": source_name if " - " not in source_name else source_name.split(" - ")[0],
                    "deadline": extract_deadline(combined),
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

        # Ensure any newly added built-in sources exist in the DB
        init_db()

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

            def _register_web_hit(item: Dict[str, str], query_tag: str, source_prefix: str = "Web RFP Search") -> None:
                u = item.get("url", "").strip()
                if not u or "grants.gov" in u.lower():
                    return
                combined = f"{item.get('title', '')} {item.get('snippet', '')}"
                if has_excluded_words(combined, exclude_words):
                    return
                matched = match_tagwords(combined, tagwords) or [query_tag]
                domain = urllib.parse.urlparse(u).netloc.removeprefix("www.")

                is_portal = any(p in combined.lower() for p in SOURCE_PORTAL_INDICATORS)
                if is_portal and settings.get("discover_new_sources", "true").lower() == "true":
                    added_src = add_source(
                        name=item["title"][:120],
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
                        "title": item["title"],
                        "url": u,
                        "source_name": f"{source_prefix} ({domain})",
                        "agency_or_funder": domain,
                        "deadline": extract_deadline(combined),
                        "snippet": item.get("snippet", ""),
                        "matched_tags": matched,
                    }

            # 1. AI-Assisted Discovery of Specialized Foundation Portals & Deep Queries
            ai_extra_queries: List[str] = []
            if settings.get("discover_new_sources", "true").lower() == "true":
                self.current_step = "Asking AI to identify specialized foundation & fellowship portals..."
                ai_disc = discover_sources_and_queries_with_ai(tagwords)
                for src_obj in ai_disc.get("sources") or []:
                    added_src = add_source(
                        name=str(src_obj.get("name", ""))[:120],
                        url=str(src_obj.get("url", "")).strip(),
                        source_type="html",
                        is_discovered=1,
                        matched_tags=", ".join(tagwords[:3]),
                        notes=str(src_obj.get("notes", "AI-recommended grant source"))[:220],
                        enabled=1,
                    )
                    if added_src:
                        new_sources.append(added_src)
                ai_extra_queries = ai_disc.get("queries") or []

            # 2. Multi-Angle Deep Web & News Search (Private Foundations, Fellowships, Directories)
            if settings.get("search_web_rfps", "true").lower() == "true":
                for kw in tagwords[:8]:
                    core_kw = " ".join(_extract_core_tokens(kw)[:4]) or kw

                    # Angle A: Private Foundation & Nonprofit RFPs (excluding Grants.gov)
                    self.current_step = f"Searching private foundations & open RFPs for '{kw}'..."
                    q_foundation = f'{core_kw} ("request for proposals" OR "call for proposals" OR "foundation grant" OR "grant opportunity") -site:grants.gov'
                    for item in web_search_ddg(q_foundation, max_results=6):
                        _register_web_hit(item, kw, "Foundation / Web RFP")

                    # Angle B: Fellowships, Research Grants, Academic & Association Awards
                    self.current_step = f"Searching fellowships, research awards & stipends for '{kw}'..."
                    q_fellowship = f'{core_kw} ("fellowship" OR "research grant" OR "seed grant" OR "stipend" OR "application deadline") -site:grants.gov'
                    for item in web_search_ddg(q_fellowship, max_results=6):
                        _register_web_hit(item, kw, "Fellowship & Research Search")

                    # Angle C: Major Grant Directories (Philanthropy News Digest, FundsforNGOs, ProFellow, GrantWatch)
                    self.current_step = f"Searching philanthropy & fellowship directories for '{kw}'..."
                    q_dirs = f'(site:philanthropynewsdigest.org/rfps OR site:fundsforngos.org OR site:profellow.com OR site:grantwatch.com OR site:instrumentl.com) {core_kw}'
                    for item in web_search_ddg(q_dirs, max_results=5):
                        _register_web_hit(item, kw, "Grant Directory")

                    # Angle D: Newly Announced Grants & RFPs in News / Press Releases
                    self.current_step = f"Checking newly announced grants & RFPs for '{kw}'..."
                    q_news = f'{core_kw} (grant OR RFP OR fellowship OR "funding opportunity" OR "accepting applications")'
                    for item in search_google_news_rss(q_news, max_results=5):
                        _register_web_hit(item, kw, "Live Grant Announcement")

                    time.sleep(0.15)

                # Run AI-recommended deep web queries
                for aq in ai_extra_queries[:6]:
                    self.current_step = f"Running AI deep-search query: '{aq[:45]}'..."
                    for item in web_search_ddg(f"{aq} -site:grants.gov", max_results=5):
                        _register_web_hit(item, aq, "AI Deep Web Search")
                    time.sleep(0.15)

            # 3. Dedicated Discovery of New Grant Sources / Portals Across the Web
            if settings.get("discover_new_sources", "true").lower() == "true":
                for kw in tagwords[:6]:
                    core_kw = " ".join(_extract_core_tokens(kw)[:4]) or kw
                    self.current_step = f"Discovering foundation directories & grant portals for '{kw}'..."
                    src_query = f'{core_kw} ("grant opportunities" OR "funding opportunities" OR "open RFPs" OR "fellowship program") -site:grants.gov'
                    src_hits = web_search_ddg(src_query, max_results=6)
                    for item in src_hits:
                        if "grants.gov" in item["url"].lower():
                            continue
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
                        _register_web_hit(item, kw, "Discovered Grant Portal")
                    time.sleep(0.15)

            # 4. Crawl Configured & Discovered Grant Sources (RSS & HTML Portals)
            active_sources = list_sources(only_enabled=True)
            auto_crawl_disc = settings.get("auto_crawl_discovered_sources", "true").lower() == "true"
            for src in active_sources[:25]:
                if src.get("is_discovered") and not auto_crawl_disc:
                    continue
                if src.get("source_type") == "api":
                    continue
                self.current_step = f"Crawling source: {src['name'][:45]}..."
                found_items = crawl_html_or_rss_source(
                    src, tagwords, rfp_indicators, exclude_words
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

            # 5. Query Additional Official Funding APIs (Federal Register NOFOs, NSF, Challenge.gov, Grants.gov)
            for kw in tagwords[:6]:
                self.current_step = f"Querying Federal Register & NSF Funding APIs for '{kw}'..."
                for h in search_federal_register_api(kw, per_page=5):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", tagwords) or [kw]
                        candidates[h["url"]] = h

                for h in search_nsf_funding_api(kw, max_rows=4):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", tagwords) or [kw]
                        candidates[h["url"]] = h

                for h in search_challenge_gov_api(kw, max_rows=4):
                    if not _url_already_seen(h["url"]) and h["url"] not in candidates:
                        h["matched_tags"] = match_tagwords(f"{h['title']} {h['snippet']}", tagwords) or [kw]
                        candidates[h["url"]] = h

            if settings.get("search_grants_gov", "true").lower() == "true":
                for kw in tagwords[:6]:
                    self.current_step = f"Querying Grants.gov API for '{kw}'..."
                    hits = search_grants_gov_api(kw, max_rows=5)
                    for h in hits:
                        if _url_already_seen(h["url"]) or h["url"] in candidates:
                            continue
                        combined = f"{h['title']} {h['snippet']} {h['agency_or_funder']}"
                        matched = match_tagwords(combined, tagwords) or [kw]
                        h["matched_tags"] = matched
                        candidates[h["url"]] = h

            # 6. Prioritize Diverse Non-Grants.gov Sources First Before AI Evaluation
            all_candidates = list(candidates.values())
            non_grants_gov = [c for c in all_candidates if "grants.gov" not in c.get("url", "").lower()]
            grants_gov_only = [c for c in all_candidates if "grants.gov" in c.get("url", "").lower()]
            candidate_list = non_grants_gov + grants_gov_only

            if candidate_list:
                self.current_step = f"Evaluating {len(candidate_list)} new grants across {len(set(c['source_name'] for c in candidate_list))} sources with AI..."
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

            # 7. Filter by Minimum AI Match Score for Notifications
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
                f"Crawled {len(candidate_list)} new candidate(s) ({len(non_grants_gov)} from web/foundations/other APIs, {len(grants_gov_only)} from Grants.gov); "
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

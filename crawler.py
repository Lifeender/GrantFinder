import re
import time
import urllib.parse
from datetime import datetime, date, timedelta
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

MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

DEADLINE_PATTERNS = [
    re.compile(
        r"(?:deadline|due date|closing date|closes|apply by|submission date|applications due|proposals due|letters of intent due|loi due)[:\s]+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|20\d{2}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+20\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2})\b",
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

# Domains that are NEVER open grant/RFP application pages
BLOCKED_DOMAINS = (
    "wikipedia.org",
    "wikimedia.org",
    "wikidata.org",
    "wiktionary.org",
    "britannica.com",
    "investopedia.com",
    "dictionary.com",
    "merriam-webster.com",
    "reddit.com",
    "quora.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "pinterest.com",
    "threads.net",
    "medium.com",
    "substack.com",
    "tumblr.com",
    "amazon.com",
    "ebay.com",
    "goodreads.com",
    "imdb.com",
    "yelp.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "simplyhired.com",
    "salary.com",
    "payscale.com",
    "researchgate.net",
    "academia.edu",
    "jstor.org",
    "arxiv.org",
    "scholar.google.com",
    "pubmed.ncbi.nlm.nih.gov",
    "doi.org",
    "sciencedirect.com",
    "springer.com",
    "wiley.com",
    "tandfonline.com",
    "mdpi.com",
    "frontiersin.org",
    "plos.org",
    "ssrn.com",
    "sagepub.com",
    "oup.com",
    "cambridge.org",
    "coursera.org",
    "udemy.com",
    "edx.org",
    "khanacademy.org",
    "chegg.com",
    "coursehero.com",
    "nytimes.com",
    "washingtonpost.com",
    "wsj.com",
    "cnn.com",
    "bbc.com",
    "bbc.co.uk",
    "theguardian.com",
    "forbes.com",
    "bloomberg.com",
    "reuters.com",
    "apnews.com",
    "npr.org",
    "politico.com",
    "thehill.com",
    "vox.com",
    "theatlantic.com",
    "newyorker.com",
    "yahoo.com",
    "msn.com",
    "news.google.com",
)

# URL path patterns that indicate navigation, encyclopedia, past winners, or non-RFP pages
BLOCKED_URL_PATTERNS = (
    "/wiki/",
    "/encyclopedia/",
    "/dictionary/",
    "/login",
    "/signin",
    "/signup",
    "/register",
    "/privacy",
    "/terms",
    "/cookie",
    "/contact",
    "/careers",
    "/jobs/",
    "/employment",
    "/press-release",
    "/news-release",
    "/media-release",
    "/past-recipients",
    "/past-grantees",
    "/past-fellows",
    "/previous-recipients",
    "/award-winners",
    "/awardees",
    "/grantee-profiles",
    "/fellow-profiles",
    "/alumni",
    "/staff",
    "/board-of-directors",
    "/annual-report",
    "/financials",
    "/donate",
    "/ways-to-give",
    "/support-us",
    "/awardsearch/showaward",
    "/ncbi.nlm.nih.gov/pmc",
)

# Phrases that explicitly indicate an opportunity is closed, expired, or not an open RFP
CLOSED_OR_NON_RFP_PHRASES = (
    "from wikipedia",
    "free encyclopedia",
    "applications are closed",
    "application period has closed",
    "application period is closed",
    "applications are currently closed",
    "this opportunity is closed",
    "opportunity is currently closed",
    "no longer accepting applications",
    "not currently accepting applications",
    "submissions are closed",
    "submission period has ended",
    "submission window has closed",
    "deadline has passed",
    "call is closed",
    "rfp is closed",
    "closed for applications",
    "cycle is closed",
    "status: closed",
    "status: archived",
    "archived opportunity",
    "funding opportunity is closed",
    "solicitation is closed",
    "this funding opportunity has been archived",
    "announces winners",
    "announces recipients",
    "announces awardees",
    "announces new fellows",
    "names new fellows",
    "congratulates the winners",
    "meet the fellows",
    "meet our fellows",
    "meet our grantees",
    "past recipients",
    "previous recipients",
    "past awardees",
)

# Explicit RFP / Grant nouns required on a candidate
RFP_GRANT_NOUNS = (
    "rfp",
    "request for proposals",
    "request for applications",
    "call for proposals",
    "call for applications",
    "funding opportunity",
    "grant opportunity",
    "grant opportunities",
    "grant program",
    "fellowship",
    "nofo",
    "nofa",
    "solicitation",
    "seed grant",
    "research grant",
    "innovation grant",
    "project grant",
    "travel grant",
    "small grant",
    "stipend",
    "letter of intent",
    "grants for",
    "funding for",
)

# Active application / submission signals required alongside an RFP/grant noun
ACTIVE_APPLICATION_SIGNALS = (
    "apply",
    "application",
    "applications",
    "deadline",
    "due date",
    "closing date",
    "closes",
    "submit",
    "submission",
    "accepting",
    "open",
    "eligibility",
    "eligible",
    "guidelines",
    "how to apply",
    "who can apply",
    "rfp",
    "request for proposals",
    "call for proposals",
    "solicitation",
    "nofo",
)


def parse_date_string(text: str) -> Optional[date]:
    """
    Parses common grant deadline date formats into a `datetime.date` object.
    Returns None if no valid full date (with 4-digit year) can be parsed.
    """
    if not text:
        return None
    s = text.strip()

    # 1. ISO YYYY-MM-DD
    m_iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", s)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            pass

    # 2. MM/DD/YYYY or MM-DD-YYYY
    m_slash = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", s)
    if m_slash:
        try:
            return date(int(m_slash.group(3)), int(m_slash.group(1)), int(m_slash.group(2)))
        except ValueError:
            pass

    # 3. Month DD, YYYY (e.g., "November 15, 2026" or "Nov. 15 2026")
    m_month_first = re.search(
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})\b", s
    )
    if m_month_first:
        mon_name = m_month_first.group(1).lower().rstrip(".")
        if mon_name in MONTH_MAP:
            try:
                return date(
                    int(m_month_first.group(3)),
                    MONTH_MAP[mon_name],
                    int(m_month_first.group(2)),
                )
            except ValueError:
                pass

    # 4. DD Month YYYY (e.g., "15 November 2026")
    m_day_first = re.search(
        r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})\b", s
    )
    if m_day_first:
        mon_name = m_day_first.group(2).lower().rstrip(".")
        if mon_name in MONTH_MAP:
            try:
                return date(
                    int(m_day_first.group(3)),
                    MONTH_MAP[mon_name],
                    int(m_day_first.group(1)),
                )
            except ValueError:
                pass

    return None


def is_deadline_expired(deadline_str: str) -> bool:
    """
    Returns True if `deadline_str` contains a parseable date that is strictly before today,
    or if it explicitly references a past year without the current or future year.
    """
    if not deadline_str:
        return False
    low = deadline_str.lower().strip()
    if low.startswith("published "):
        # A publication date is not a closing deadline, but check if published > 120 days ago
        pub_d = parse_date_string(deadline_str)
        if pub_d and pub_d < (datetime.now().date() - timedelta(days=120)):
            return True
        return False

    parsed = parse_date_string(deadline_str)
    today = datetime.now().date()
    if parsed:
        return parsed < today

    # Check if deadline string mentions a 4-digit year strictly in the past
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", deadline_str)]
    if years and max(years) < today.year:
        return True
    return False


def has_stale_year_in_title_or_url(title: str, url: str) -> bool:
    """
    Detects if a candidate's title or URL explicitly belongs to a past grant cycle
    (e.g., '2022 Request for Proposals' or '/2023/rfp') without referencing the current or next year.
    """
    current_year = datetime.now().year
    combined = f"{title or ''} {url or ''}"
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", combined)]
    if not years:
        return False
    # If the highest year mentioned in the title/URL is strictly less than current_year, it's stale
    if max(years) < current_year:
        return True
    return False


def extract_deadline(text: str) -> str:
    """
    Extracts a deadline string from text only if it is NOT expired.
    If an extracted deadline is already in the past, returns "EXPIRED" sentinel so the caller can reject it.
    """
    if not text:
        return ""
    for pat in DEADLINE_PATTERNS:
        for m in pat.finditer(text):
            candidate_dl = m.group(1).strip()
            if is_deadline_expired(candidate_dl):
                return "EXPIRED"
            return candidate_dl
    return ""


def is_valid_rfp_url(url: str, title: str = "") -> bool:
    """
    Verifies that a URL and title do not belong to Wikipedia, social media, job boards,
    academic article repositories, news outlets, or non-RFP navigation paths.
    """
    u = (url or "").strip().lower()
    t = (title or "").strip().lower()
    if not u.startswith("http"):
        return False
    try:
        parsed = urllib.parse.urlparse(u)
        host = parsed.netloc.removeprefix("www.")
    except Exception:
        return False

    if not host:
        return False

    for bad_dom in BLOCKED_DOMAINS:
        if host == bad_dom or host.endswith("." + bad_dom):
            return False

    for bad_pat in BLOCKED_URL_PATTERNS:
        if bad_pat in u:
            return False

    if "wikipedia" in t or "free encyclopedia" in t:
        return False

    if has_stale_year_in_title_or_url(title, url):
        return False

    return True


def is_active_rfp_candidate(
    title: str,
    snippet: str,
    url: str,
    deadline: str = "",
    exclude_words: Optional[List[str]] = None,
) -> bool:
    """
    Strict multi-check gatekeeper ensuring a candidate looks like a genuine,
    currently active/open grant, fellowship, or RFP before it can enter the pipeline.
    """
    if not is_valid_rfp_url(url, title):
        return False

    combined = f"{title or ''} {snippet or ''}".lower()
    if exclude_words and has_excluded_words(combined, exclude_words):
        return False

    for phrase in CLOSED_OR_NON_RFP_PHRASES:
        if phrase in combined:
            return False

    if deadline == "EXPIRED" or (deadline and is_deadline_expired(deadline)):
        return False

    extracted_dl = extract_deadline(combined)
    if extracted_dl == "EXPIRED":
        return False

    # Must contain at least one explicit grant/RFP noun AND at least one active application signal
    has_grant_noun = any(noun in combined for noun in RFP_GRANT_NOUNS)
    has_app_signal = any(sig in combined for sig in ACTIVE_APPLICATION_SIGNALS)
    if not (has_grant_noun and has_app_signal):
        return False

    return True


def verify_and_enrich_live_rfp(candidate: Dict[str, Any], exclude_words: List[str]) -> bool:
    """
    Fetches the live candidate webpage (for non-API web/directory candidates) to verify:
    1. The link is alive (HTTP 200) and doesn't redirect to a blocked domain (like Wikipedia or a news site).
    2. The live page does not state that applications are closed or the deadline has passed.
    3. Any deadline on the page is in the future (not expired).
    4. The page contains genuine grant/RFP application details.
    Mutates `candidate` in-place with resolved URL and extracted deadline if valid.
    """
    url = candidate.get("url", "").strip()
    source_name = candidate.get("source_name", "")

    # Official APIs that already validated status & expiration don't need an extra HTML scrape
    if any(
        api_src in source_name
        for api_src in ("Grants.gov API", "Challenge.gov API", "Federal Register")
    ):
        dl = candidate.get("deadline", "")
        return not is_deadline_expired(dl)

    if not is_valid_rfp_url(url, candidate.get("title", "")):
        return False

    try:
        resp = requests.get(url, headers=HEADERS, timeout=6, allow_redirects=True)
        if resp.status_code != 200:
            return False
        final_url = resp.url.strip()
        if not is_valid_rfp_url(final_url, candidate.get("title", "")):
            return False
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" in content_type:
            # Direct PDF RFP solicitation link is valid if URL & snippet passed checks
            candidate["url"] = final_url
            return True
        if "html" not in content_type and "xml" not in content_type:
            return False

        soup = BeautifulSoup(resp.text[:120000], "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()

        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if not is_valid_rfp_url(final_url, page_title):
            return False

        body_text = soup.get_text(" ", strip=True)[:8000]
        body_low = body_text.lower()

        if has_excluded_words(body_low[:2500], exclude_words):
            return False

        # Reject if page explicitly states opportunity is closed/expired
        for closed_phrase in CLOSED_OR_NON_RFP_PHRASES:
            if closed_phrase in body_low[:4000]:
                return False

        # Check deadline on the live page
        page_dl = extract_deadline(body_text[:5000])
        if page_dl == "EXPIRED":
            return False
        if page_dl and not candidate.get("deadline"):
            candidate["deadline"] = page_dl

        if candidate.get("deadline") and is_deadline_expired(candidate["deadline"]):
            return False

        # Require the live page to have both grant/RFP nouns and application/eligibility language
        has_noun = any(n in body_low for n in RFP_GRANT_NOUNS)
        has_app = any(
            s in body_low
            for s in ("apply", "application", "deadline", "due date", "eligibility", "submit", "proposal", "guidelines")
        )
        if not (has_noun and has_app):
            return False

        candidate["url"] = final_url
        return True
    except Exception:
        return False


def _extract_core_tokens(phrase: str) -> List[str]:
    words = re.findall(r"[a-z0-9]{3,}", (phrase or "").lower())
    core = [w for w in words if w not in GENERIC_QUERY_STOPWORDS]
    return core if core else words


def match_tagwords(text: str, tagwords: List[str]) -> List[str]:
    """
    Matches candidate grant text against active search queries using both
    exact phrase matching and smart core-token matching so multi-word AI queries
    accurately match real foundation, RSS, and web grant listings.
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
        elif hits >= 1 and any(w in low for w in ("grant", "fellowship", "rfp", "award", "funding", "stipend", "solicitation")):
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
    Queries the official Grants.gov REST API v1 strictly for actively posted grants
    whose closing date has not passed.
    """
    api_url = "https://api.grants.gov/v1/api/search2"
    payload = {
        "rows": max_rows,
        "keyword": keyword,
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
                snippet = f"Opportunity #{number} | Status: {doc_type} | Opened: {open_date} | Closes: {close_date or 'Open'}"
                found.append(
                    {
                        "title": title,
                        "url": url,
                        "source_name": "Grants.gov API",
                        "agency_or_funder": agency,
                        "deadline": close_date or "Open / Rolling",
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
    Filters out OMB paperwork/comment notices and expired notices.
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

                dl = extract_deadline(abstract)
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
        dl = extract_deadline(f"{title} {snippet}")
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
                        "deadline": end_date or "Open Challenge",
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
    filtered against blocked non-RFP domains.
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
                if title and is_valid_rfp_url(link, title):
                    results.append({"title": title, "url": link, "snippet": desc})
    except Exception:
        pass
    return results


def web_search_ddg(query: str, max_results: int = 8) -> List[Dict[str, str]]:
    """
    Multi-engine web search combining DDGS, Bing RSS, and DuckDuckGo HTML,
    strictly filtering out Wikipedia, encyclopedias, social media, and stale pages.
    """
    seen_urls = set()
    results: List[Dict[str, str]] = []

    def _add_hit(title: str, url: str, snippet: str) -> None:
        u = (url or "").strip()
        t = (title or "").strip()
        if not u or not t or u in seen_urls:
            return
        if not is_valid_rfp_url(u, t):
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
    auto-detects embedded RSS feeds if present, and extracts ONLY active/open
    grant/RFP items matching the user's search topics.
    """
    url = source["url"]
    source_name = source["name"]
    discovered_grants: List[Dict[str, Any]] = []

    if source.get("source_type") == "api" or "grants.gov" in url.lower():
        return []
    if not is_valid_rfp_url(url, source_name):
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
            dl = extract_deadline(combined)
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
        if len(title) < 12:
            continue

        parent = a.find_parent(["article", "li", "div", "tr", "section"])
        context_text = parent.get_text(" ", strip=True)[:650] if parent else title
        combined = f"{title} {context_text}"
        dl = extract_deadline(combined)

        if not is_active_rfp_candidate(title, context_text, full_url, dl, exclude_words):
            continue

        matched = match_tagwords(combined, tagwords)
        if matched and has_rfp_indicator(combined, rfp_indicators):
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
        self.current_step = "Starting Verified Active Grant & RFP Search..."
        started_at = datetime.now().isoformat(timespec="seconds")

        # Ensure built-in sources exist and purge any invalid/Wikipedia/expired entries
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

            current_year = datetime.now().year
            next_year = current_year + 1

            def _register_web_hit(item: Dict[str, str], query_tag: str, source_prefix: str = "Web RFP Search") -> None:
                u = item.get("url", "").strip()
                t = item.get("title", "").strip()
                snip = item.get("snippet", "").strip()
                if not u or "grants.gov" in u.lower():
                    return
                combined = f"{t} {snip}"
                dl = extract_deadline(combined)
                if not is_active_rfp_candidate(t, snip, u, dl, exclude_words):
                    return

                matched = match_tagwords(combined, tagwords) or [query_tag]
                domain = urllib.parse.urlparse(u).netloc.removeprefix("www.")

                is_portal = any(p in combined.lower() for p in SOURCE_PORTAL_INDICATORS)
                if is_portal and settings.get("discover_new_sources", "true").lower() == "true":
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

            # 1. AI-Assisted Discovery of Specialized Foundation Portals & Deep Queries
            ai_extra_queries: List[str] = []
            if settings.get("discover_new_sources", "true").lower() == "true":
                self.current_step = "Asking AI to identify specialized foundation & fellowship RFP portals..."
                ai_disc = discover_sources_and_queries_with_ai(tagwords)
                for src_obj in ai_disc.get("sources") or []:
                    src_url = str(src_obj.get("url", "")).strip()
                    src_name = str(src_obj.get("name", ""))[:120]
                    if not is_valid_rfp_url(src_url, src_name):
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

            # 2. Multi-Angle Deep Web Search Strictly for Active RFPs (Excluding Wikipedia, News, & Grants.gov)
            neg_filters = "-site:grants.gov -site:wikipedia.org -site:reddit.com -site:youtube.com -site:nytimes.com"
            if settings.get("search_web_rfps", "true").lower() == "true":
                for kw in tagwords[:8]:
                    core_kw = " ".join(_extract_core_tokens(kw)[:4]) or kw

                    # Angle A: Open Private Foundation & Nonprofit RFPs with Active Application Terms
                    self.current_step = f"Searching open foundation RFPs & calls for proposals for '{kw}'..."
                    q_foundation = (
                        f'{core_kw} ("request for proposals" OR "call for proposals" OR "open RFP" OR "grant application") '
                        f'("apply by" OR "deadline" OR "{current_year}" OR "{next_year}") {neg_filters}'
                    )
                    for item in web_search_ddg(q_foundation, max_results=6):
                        _register_web_hit(item, kw, "Foundation / Open RFP")

                    # Angle B: Active Fellowships, Research Grants & Stipends Accepting Applications
                    self.current_step = f"Searching active fellowships & research grants for '{kw}'..."
                    q_fellowship = (
                        f'{core_kw} ("fellowship application" OR "research grant" OR "seed grant" OR "call for applications") '
                        f'("deadline" OR "accepting applications" OR "how to apply") {neg_filters}'
                    )
                    for item in web_search_ddg(q_fellowship, max_results=6):
                        _register_web_hit(item, kw, "Fellowship & Research RFP")

                    # Angle C: Dedicated Open RFP Directories (Philanthropy News Digest RFPs, FundsforNGOs, ProFellow, Instrumentl)
                    self.current_step = f"Searching verified RFP directories for '{kw}'..."
                    q_dirs = (
                        f'(site:philanthropynewsdigest.org/rfps OR site:fundsforngos.org OR site:profellow.com OR site:instrumentl.com/grants) '
                        f'{core_kw} ("deadline" OR "apply" OR "{current_year}" OR "{next_year}")'
                    )
                    for item in web_search_ddg(q_dirs, max_results=5):
                        _register_web_hit(item, kw, "Verified Grant Directory")

                    time.sleep(0.15)

                # Run AI-recommended deep web queries with strict RFP + exclusion filters
                for aq in ai_extra_queries[:6]:
                    self.current_step = f"Running AI deep-search query: '{aq[:45]}'..."
                    strict_aq = f'{aq} ("apply" OR "deadline" OR "RFP" OR "funding opportunity") {neg_filters}'
                    for item in web_search_ddg(strict_aq, max_results=5):
                        _register_web_hit(item, aq, "AI Deep RFP Search")
                    time.sleep(0.15)

            # 3. Crawl Configured & Discovered Grant Sources (RSS & HTML Portals)
            active_sources = list_sources(only_enabled=True)
            auto_crawl_disc = settings.get("auto_crawl_discovered_sources", "true").lower() == "true"
            for src in active_sources[:25]:
                if src.get("is_discovered") and not auto_crawl_disc:
                    continue
                if src.get("source_type") == "api":
                    continue
                if not is_valid_rfp_url(src.get("url", ""), src.get("name", "")):
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

            # 4. Query Official Funding APIs (Federal Register NOFOs, NSF Active Solicitations, Challenge.gov, Grants.gov)
            for kw in tagwords[:6]:
                self.current_step = f"Querying Federal Register, NSF & Challenge.gov APIs for '{kw}'..."
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
                    self.current_step = f"Querying Grants.gov API for active posted grants in '{kw}'..."
                    hits = search_grants_gov_api(kw, max_rows=5)
                    for h in hits:
                        if _url_already_seen(h["url"]) or h["url"] in candidates:
                            continue
                        combined = f"{h['title']} {h['snippet']} {h['agency_or_funder']}"
                        matched = match_tagwords(combined, tagwords) or [kw]
                        h["matched_tags"] = matched
                        candidates[h["url"]] = h

            # 5. Prioritize Non-Grants.gov Sources & Verify Live Target Pages Before AI Evaluation
            all_candidates = list(candidates.values())
            non_grants_gov = [c for c in all_candidates if "grants.gov" not in c.get("url", "").lower()]
            grants_gov_only = [c for c in all_candidates if "grants.gov" in c.get("url", "").lower()]
            ordered_candidates = non_grants_gov + grants_gov_only

            verified_candidates: List[Dict[str, Any]] = []
            for idx, cand in enumerate(ordered_candidates[:55]):
                self.current_step = f"Verifying live RFP page ({idx + 1}/{min(len(ordered_candidates), 55)}): {cand['title'][:40]}..."
                if verify_and_enrich_live_rfp(cand, exclude_words):
                    verified_candidates.append(cand)

            min_score = int(settings.get("ai_min_match_score", "65") or 65)

            # 6. AI Relevance & Active-RFP Gatekeeper — Only Save Verified Active RFPs Meeting Min Score
            if verified_candidates:
                self.current_step = f"Evaluating {len(verified_candidates)} verified active RFPs with AI..."
                scored_candidates = evaluate_grants_batch(verified_candidates)
                for c in scored_candidates:
                    score = int(c.get("ai_score", 0))
                    is_active = bool(c.get("is_active_rfp", True))
                    if not is_active or score < min_score:
                        continue
                    if not is_valid_rfp_url(c.get("url", ""), c.get("title", "")):
                        continue
                    if c.get("deadline") and is_deadline_expired(c["deadline"]):
                        continue
                    inserted = insert_grant_if_new(
                        title=c["title"],
                        url=c["url"],
                        source_name=c["source_name"],
                        matched_tags=c["matched_tags"],
                        agency_or_funder=c.get("agency_or_funder", ""),
                        deadline=c.get("deadline", ""),
                        snippet=c.get("snippet", ""),
                        ai_score=score,
                        ai_reason=c.get("ai_reason", ""),
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
                f"Inspected {len(ordered_candidates)} candidate link(s), verified {len(verified_candidates)} live open RFP page(s), "
                f"and saved {len(high_match_grants)} active RFP match(es) (>={min_score}%). "
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

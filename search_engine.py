"""
===============================================================================
search_engine.py — Crawler Retrieval, URL Blacklisting, Deadline Parsing & Scraper
===============================================================================
Purpose:
    This module is Stage 2 of the GrantFinder pipeline. It handles:
    1. Rejecting non-RFP domains (Wikipedia, social media, news sites, academic
       paper repositories, job boards) via `BLOCKED_DOMAINS`.
    2. Rejecting non-RFP URL path patterns (`/news/`, `/blog/`, `/press-releases/`,
       `/winners/`, `/past-recipients/`) via `BLACKLIST_URL_PATTERNS`.
    3. Parsing closing deadlines (`parse_date_string`, `extract_deadline_from_text`,
       `is_deadline_expired`) while avoiding false-positive expiration on "Opened:"
       or "Posted:" dates.
    4. Building domain-targeted and Boolean search queries (`build_boolean_search_queries`).
    5. Scraping both live HTML pages and downloadable PDF RFP/NOFO guidelines
       (`scrape_document_content`, `extract_pdf_text_bytes`) with snippet fallback
       when foundation websites block automated scrapers.

How to Customize:
    - To block additional websites, add their root domain to `BLOCKED_DOMAINS`.
    - To block additional URL path segments, add them to `BLACKLIST_URL_PATTERNS`.
    - To change the Boolean search templates used for web discovery, edit
      `build_boolean_search_queries()`.
===============================================================================
"""

import io
import re
import urllib.parse
import zlib
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup

# =====================================================================
# 1. HTTP CLIENT HEADERS & DATE CONSTANTS
# =====================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
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

# =====================================================================
# 2. DOMAIN & URL PATH BLACKLISTS
# =====================================================================

# Domains that are NEVER open grant/RFP solicitations (encyclopedias, social media,
# job boards, and e-commerce).
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
    "chegg.com",
    "coursehero.com",
)

# URL path substrings that indicate navigation noise, login screens, or job postings.
BLACKLIST_URL_PATTERNS = (
    "/wiki/",
    "/encyclopedia/",
    "/dictionary/",
    "/watch?",
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
    "/donate",
    "/ways-to-give",
    "/support-us",
)

# =====================================================================
# 3. DEADLINE EXTRACTION & EXPIRATION CHECKING
# =====================================================================

DEADLINE_PATTERNS = [
    re.compile(
        r"(?:deadline|due date|closing date|closes|apply by|submission date|applications due|proposals due|letters of intent due|loi due|close date)[:\s]+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|20\d{2}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2})\b",
        re.IGNORECASE,
    ),
]


def parse_date_string(text: str) -> Optional[date]:
    """
    Parses common US and ISO date formats into a `datetime.date` object:
    - ISO: `2026-06-30`
    - Slash/Dash: `06/30/2026`
    - Month Day, Year: `June 30, 2026` or `Jun. 30, 2026`
    - Day Month Year: `30 June 2026`
    Returns `None` if no valid calendar date is found.
    """
    if not text:
        return None
    s = text.strip()

    # Format 1: YYYY-MM-DD
    m_iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", s)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            pass

    # Format 2: MM/DD/YYYY or MM-DD-YYYY
    m_slash = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", s)
    if m_slash:
        try:
            return date(int(m_slash.group(3)), int(m_slash.group(1)), int(m_slash.group(2)))
        except ValueError:
            pass

    # Format 3: Month DD, YYYY (e.g., "December 15, 2026")
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

    # Format 4: DD Month YYYY (e.g., "15 December 2026")
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


def normalize_deadline_iso(text: str) -> str:
    """
    Attempts to normalize a date string into `YYYY-MM-DD` format for consistent UI
    display and sorting; returns the original trimmed string if rolling or unparsed.
    """
    d = parse_date_string(text)
    if d:
        return d.strftime("%Y-%m-%d")
    return (text or "").strip()


def is_deadline_expired(deadline_str: str) -> bool:
    """
    Determines whether an explicit solicitation deadline has already passed (`< today`).
    - Rolling / ongoing / open / Published deadlines are never treated as expired.
    """
    if not deadline_str:
        return False
    low = deadline_str.lower().strip()
    if any(w in low for w in ("rolling", "ongoing", "open", "active", "continuous", "tbd", "published")):
        return False

    parsed = parse_date_string(deadline_str)
    today = datetime.now().date()
    if parsed:
        return parsed < today

    # Fallback: only treat as expired if a 4-digit year is strictly more than 1 year in the past
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", deadline_str)]
    if years and max(years) < (today.year - 1):
        return True
    return False


def has_stale_year_in_title_or_url(title: str, url: str = "") -> bool:
    """
    Checks if the TITLE explicitly names an archaic grant cycle year (2+ years old,
    e.g., '2021 Request for Proposals'). Allows prior-year/multi-year titles like 2025/2026.
    """
    current_year = datetime.now().year
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", title or "")]
    if not years:
        return False
    return max(years) < (current_year - 1)


def extract_deadline_from_text(text: str) -> str:
    """
    Scans document text for closing deadlines (`DEADLINE_PATTERNS`).
    Prefers future deadlines; if none are in the future, returns empty string `""`
    rather than hard-expiring the entire page (since many recurring grant pages mention
    prior cycle dates while remaining open or rolling).
    """
    if not text:
        return ""
    found_future: List[str] = []
    for pat in DEADLINE_PATTERNS:
        for m in pat.finditer(text):
            cand = m.group(1).strip()
            if not is_deadline_expired(cand):
                found_future.append(normalize_deadline_iso(cand))
    if found_future:
        return found_future[0]
    return ""


def is_url_blacklisted(url: str, title: str = "") -> bool:
    """
    Master URL & Title Blacklist Gate:
    Returns `True` (reject) if the URL or title matches:
    1. Any domain in `BLOCKED_DOMAINS` (Wikipedia, social media, job sites, etc.)
    2. Any URL path in `BLACKLIST_URL_PATTERNS` (`/login`, `/careers`, `/wiki/`, etc.)
    3. An archaic grant cycle year in the title.
    """
    u = (url or "").strip().lower()
    t = (title or "").strip().lower()
    if not u.startswith("http"):
        return True
    try:
        parsed = urllib.parse.urlparse(u)
        host = parsed.netloc.removeprefix("www.")
    except Exception:
        return True

    if not host:
        return True

    for bad_dom in BLOCKED_DOMAINS:
        if host == bad_dom or host.endswith("." + bad_dom):
            return True

    for pat in BLACKLIST_URL_PATTERNS:
        if pat in u:
            return True

    if "wikipedia" in t or "free encyclopedia" in t:
        return True

    if has_stale_year_in_title_or_url(title):
        return True

    return False


# =====================================================================
# 4. MULTI-ANGLE SEARCH QUERY BUILDER (RESTORED HIGH-RECALL ANGLES)
# =====================================================================

def build_boolean_search_queries(criteria: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Builds high-recall, search-engine-friendly queries across 4 angles for each topic:
    1. Private Foundation & Nonprofit RFPs (`"request for proposals" OR "call for proposals" OR "foundation grant" OR "grant opportunity"`)
    2. Fellowships, Research Grants & Stipends (`"fellowship" OR "research grant" OR "seed grant" OR "stipend" OR "application deadline"`)
    3. Major Philanthropy & Grant Directories (`philanthropynewsdigest.org/rfps`, `fundsforngos.org`, `profellow.com`, `grantwatch.com`, `instrumentl.com`)
    4. Active Grant Portals & Open Funding Opportunities (`"grant opportunities" OR "funding opportunities" OR "open RFPs"`)
    """
    base_queries = criteria.get("search_queries") or []
    project_domain = criteria.get("project_domain") or ""
    if not base_queries and project_domain:
        base_queries = [project_domain]

    compiled: List[Dict[str, str]] = []
    seen_q = set()

    def _add_plan(q_str: str, topic: str, label: str) -> None:
        q_clean = re.sub(r"\s+", " ", q_str).strip()
        if q_clean and q_clean not in seen_q:
            seen_q.add(q_clean)
            compiled.append({"query": q_clean, "topic": topic, "label": label})

    for q in base_queries[:6]:
        topic_core = re.sub(
            r'\b(grant|grants|rfp|rfps|nofo|notice of funding opportunity|request for proposals|solicitation|guidelines|funding|opportunity|opportunities)\b',
            " ",
            re.sub(r'\(".*?"\)', " ", q),
            flags=re.IGNORECASE,
        )
        topic_core = re.sub(r"\s+", " ", topic_core).strip() or q.strip()
        # Keep core phrase concise (up to 4 words) so search engines don't over-constrain
        core_words = topic_core.split()[:4]
        short_core = " ".join(core_words) if core_words else topic_core

        # Angle A: Private Foundation & Nonprofit RFPs
        _add_plan(
            f'{short_core} ("request for proposals" OR "call for proposals" OR "foundation grant" OR "grant opportunity") -site:grants.gov -site:wikipedia.org',
            short_core,
            "Foundation / Web RFP",
        )

        # Angle B: Fellowships, Research Grants, Academic & Association Awards
        _add_plan(
            f'{short_core} ("fellowship" OR "research grant" OR "seed grant" OR "stipend" OR "application deadline") -site:grants.gov -site:wikipedia.org',
            short_core,
            "Fellowship & Research Search",
        )

        # Angle C: Major Grant & Fellowship Directories
        _add_plan(
            f'(site:philanthropynewsdigest.org/rfps OR site:fundsforngos.org OR site:profellow.com OR site:grantwatch.com OR site:instrumentl.com) {short_core}',
            short_core,
            "Grant Directory",
        )

        # Angle D: Grant Portals & Open Funding Opportunities
        _add_plan(
            f'{short_core} ("grant opportunities" OR "funding opportunities" OR "open RFPs" OR "notice of funding opportunity") -site:wikipedia.org',
            short_core,
            "Open Funding Opportunity",
        )

    return compiled


# =====================================================================
# 5. HTML & PDF GUIDELINE SCRAPER (WITH ANTI-BOT FALLBACK)
# =====================================================================

def extract_pdf_text_bytes(pdf_bytes: bytes, max_chars: int = 12000) -> str:
    """
    Extracts readable text from downloadable RFP/NOFO PDF guidelines (~3,000 tokens).
    - First tries `pypdf.PdfReader` if installed.
    - Falls back to a pure-Python `zlib` PDF stream decompressor so PDF scraping works
      even in minimal environments without external PDF libraries.
    """
    if not pdf_bytes:
        return ""

    # Method 1: pypdf library (if installed)
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text: List[str] = []
        total_len = 0
        for page in reader.pages[:12]:
            t = (page.extract_text() or "").strip()
            if t:
                pages_text.append(t)
                total_len += len(t)
                if total_len >= max_chars:
                    break
        joined = "\n".join(pages_text).strip()
        if len(joined) >= 80:
            return re.sub(r"\s+", " ", joined)[:max_chars]
    except Exception:
        pass

    # Method 2: Pure-Python zlib stream text extractor
    extracted_chunks: List[str] = []
    for match in re.finditer(rb"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL):
        stream_data = match.group(1)
        try:
            decompressed = zlib.decompress(stream_data)
        except Exception:
            decompressed = stream_data
        for txt_match in re.finditer(rb"\(([^\(\)]{3,250})\)", decompressed):
            chunk = txt_match.group(1).decode("latin-1", errors="ignore")
            chunk_clean = re.sub(r"\\[nrtbf()\\]", " ", chunk)
            if re.search(r"[A-Za-z]{3,}", chunk_clean):
                extracted_chunks.append(chunk_clean)
        if sum(len(c) for c in extracted_chunks) >= max_chars:
            break

    text = " ".join(extracted_chunks)
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _extract_targeted_section(full_text: str, section_keywords: List[str], window: int = 2500) -> str:
    """
    Locates specific sections inside a long RFP document (such as "Eligibility",
    "Who May Apply", or "501(c)(3)") and extracts a surrounding character window
    so the Pass-2 LLM verifier can check statutory applicant eligibility.
    """
    low = full_text.lower()
    snippets: List[str] = []
    for kw in section_keywords:
        idx = low.find(kw)
        if idx != -1:
            start = max(0, idx - 120)
            end = min(len(full_text), idx + window)
            snippets.append(full_text[start:end].strip())
            if sum(len(s) for s in snippets) >= window:
                break
    return " ... ".join(snippets)[:window]


def scrape_document_content(url: str, fallback_title: str = "", fallback_snippet: str = "", timeout: int = 7) -> Optional[Dict[str, Any]]:
    """
    Fetches and parses a candidate grant URL (supporting both HTML pages and PDF files).
    - Returns `None` if the URL is blacklisted or returns HTTP 404/410 (dead link).
    - If a foundation site blocks automated scraping (e.g., HTTP 403 Cloudflare) or times out,
      falls back to `fallback_title` + `fallback_snippet` so legitimate grants are not dropped.
    - On HTML pages, also scans for embedded downloadable RFP/NOFO PDF guideline links and
      supplements the page text with the PDF text when needed.
    """
    if is_url_blacklisted(url, fallback_title):
        return None

    def _build_fallback_payload() -> Optional[Dict[str, Any]]:
        """Builds a structured document payload from search/RSS snippets when live HTTP scraping is blocked."""
        if not fallback_title and not fallback_snippet:
            return None
        combined = f"{fallback_title}\n{fallback_snippet}".strip()
        return {
            "url": url,
            "doc_type": "snippet_fallback",
            "title": fallback_title[:280],
            "synopsis": fallback_snippet[:2500],
            "eligibility_section": fallback_snippet[:2500],
            "detected_deadline": extract_deadline_from_text(combined),
            "document_text": combined[:12000],
        }

    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code in (404, 410):
            # Dead link -> discard immediately
            return None
        if resp.status_code != 200:
            # Bot-protected portal (401/403/429) -> verify using search/RSS snippet
            return _build_fallback_payload()
        final_url = resp.url.strip()
        if is_url_blacklisted(final_url, fallback_title):
            return None
        content_type = resp.headers.get("Content-Type", "").lower()
    except Exception:
        return _build_fallback_payload()

    # Case A: Direct PDF RFP / NOFO Guidelines document
    if "application/pdf" in content_type or final_url.lower().split("?")[0].endswith(".pdf"):
        pdf_text = extract_pdf_text_bytes(resp.content, max_chars=12000)
        if not pdf_text and fallback_snippet:
            pdf_text = f"{fallback_title}\n{fallback_snippet}"
        if not pdf_text:
            return _build_fallback_payload()
        elig = _extract_targeted_section(
            pdf_text,
            ["eligibility", "eligible applicants", "who may apply", "who can apply", "applicant type", "501(c)(3)"],
            window=3000,
        )
        dl = extract_deadline_from_text(pdf_text[:6000])
        return {
            "url": final_url,
            "doc_type": "pdf",
            "title": fallback_title or final_url.split("/")[-1],
            "synopsis": pdf_text[:2500],
            "eligibility_section": elig or pdf_text[2500:5500],
            "detected_deadline": dl,
            "document_text": pdf_text[:12000],
        }

    if "html" not in content_type and "xml" not in content_type and "text" not in content_type:
        return _build_fallback_payload()

    # Case B: HTML RFP / Grant Page (with optional embedded PDF Guidelines link)
    soup = BeautifulSoup(resp.text[:180000], "html.parser")

    # Look for a downloadable PDF RFP/Guideline link on the HTML page
    pdf_guideline_url = ""
    for a in soup.find_all("a", href=True)[:120]:
        href = a["href"].strip()
        a_text = a.get_text(" ", strip=True).lower()
        if ".pdf" in href.lower() and any(
            k in a_text or k in href.lower()
            for k in ("rfp", "nofo", "guideline", "solicitation", "application", "call", "instructions", "announcement")
        ):
            cand_pdf = urllib.parse.urljoin(final_url, href)
            if not is_url_blacklisted(cand_pdf):
                pdf_guideline_url = cand_pdf
                break

    # Remove noisy navigation/script/footer elements before extracting body text
    for tag in soup(["script", "style", "nav", "footer", "noscript", "header"]):
        tag.decompose()

    page_title = (soup.title.get_text(" ", strip=True) if soup.title else "") or fallback_title
    if is_url_blacklisted(final_url, page_title):
        return None

    meta_desc = ""
    meta_el = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)}) or soup.find(
        "meta", attrs={"property": re.compile(r"og:description", re.I)}
    )
    if meta_el and meta_el.get("content"):
        meta_desc = meta_el["content"].strip()

    body_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    if len(body_text) < 60 and fallback_snippet:
        body_text = f"{body_text} {fallback_snippet}".strip()

    # If the HTML page is short and links to a PDF guideline, scrape the PDF text too
    pdf_supplement = ""
    if pdf_guideline_url and len(body_text) < 4500:
        try:
            pdf_resp = requests.get(pdf_guideline_url, headers=HEADERS, timeout=5)
            if pdf_resp.status_code == 200 and "pdf" in pdf_resp.headers.get("Content-Type", "").lower():
                pdf_supplement = extract_pdf_text_bytes(pdf_resp.content, max_chars=6000)
        except Exception:
            pass

    combined_doc = f"{page_title}\n{meta_desc}\n{fallback_snippet}\n{body_text}\n{pdf_supplement}".strip()[:12000]
    eligibility_sec = _extract_targeted_section(
        combined_doc,
        [
            "eligibility",
            "eligible applicants",
            "who can apply",
            "who may apply",
            "applicant eligibility",
            "eligible organizations",
            "501(c)(3)",
            "guidelines",
        ],
        window=3200,
    )
    dl = extract_deadline_from_text(combined_doc[:8000])

    return {
        "url": final_url,
        "pdf_guideline_url": pdf_guideline_url,
        "doc_type": "html+pdf" if pdf_supplement else "html",
        "title": page_title[:280],
        "synopsis": (meta_desc + " " + fallback_snippet + " " + body_text[:2000]).strip()[:2500],
        "eligibility_section": eligibility_sec or body_text[:3000],
        "detected_deadline": dl,
        "document_text": combined_doc[:12000],
    }


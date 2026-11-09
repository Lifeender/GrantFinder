import io
import re
import urllib.parse
import zlib
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional
import requests
from bs4 import BeautifulSoup

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

# Domains that are never open grant/RFP solicitations
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

# URL path blacklist patterns to eliminate news blogs, press releases, past winners, and non-RFPs
BLACKLIST_URL_PATTERNS = (
    "/news/",
    "/blog/",
    "/blogs/",
    "/press-releases/",
    "/press-release/",
    "/news-release/",
    "/media-release/",
    "/winners/",
    "/award-winners/",
    "/past-recipients/",
    "/past-grantees/",
    "/past-fellows/",
    "/previous-recipients/",
    "/awardees/",
    "/grantee-profiles/",
    "/fellow-profiles/",
    "/alumni/",
    "/wiki/",
    "/encyclopedia/",
    "/dictionary/",
    "/podcast/",
    "/video/",
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

# ONLY match dates explicitly labeled as a deadline / closing / due date (never match 'Opened:' or 'Posted:')
DEADLINE_PATTERNS = [
    re.compile(
        r"(?:deadline|due date|closing date|closes|apply by|submission date|applications due|proposals due|letters of intent due|loi due|close date)[:\s]+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|20\d{2}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})",
        re.IGNORECASE,
    ),
]


def parse_date_string(text: str) -> Optional[date]:
    if not text:
        return None
    s = text.strip()

    m_iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", s)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            pass

    m_slash = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", s)
    if m_slash:
        try:
            return date(int(m_slash.group(3)), int(m_slash.group(1)), int(m_slash.group(2)))
        except ValueError:
            pass

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
    Attempts to parse a date string into YYYY-MM-DD format; returns original string if rolling/unparsed.
    """
    d = parse_date_string(text)
    if d:
        return d.strftime("%Y-%m-%d")
    return (text or "").strip()


def is_deadline_expired(deadline_str: str) -> bool:
    if not deadline_str:
        return False
    low = deadline_str.lower().strip()
    if any(w in low for w in ("rolling", "ongoing", "open", "active", "continuous", "tbd")):
        return False
    if low.startswith("published "):
        pub_d = parse_date_string(deadline_str)
        if pub_d and pub_d < (datetime.now().date() - timedelta(days=150)):
            return True
        return False

    parsed = parse_date_string(deadline_str)
    today = datetime.now().date()
    if parsed:
        return parsed < today

    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", deadline_str)]
    if years and max(years) < today.year:
        return True
    return False


def has_stale_year_in_title_or_url(title: str, url: str = "") -> bool:
    """
    Checks if the TITLE explicitly names a past grant cycle year (e.g. '2022 Request for Proposals').
    Does NOT inspect numeric substrings in URLs because grant IDs and multi-year NOFO archive paths
    frequently contain numbers matching 20xx.
    """
    current_year = datetime.now().year
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", title or "")]
    if not years:
        return False
    # Reject if the title explicitly mentions a year strictly before (current_year - 1)
    # or before current_year without mentioning current_year / next_year
    return max(years) < current_year


def extract_deadline_from_text(text: str) -> str:
    """
    Extracts explicit closing deadlines from text.
    If multiple deadline dates are mentioned (e.g. Cycle 1 in the past, Cycle 2 in the future),
    prefers the active/future deadline. Returns 'EXPIRED' only if explicit deadline date(s) were
    found and ALL of them are in the past.
    """
    if not text:
        return ""
    found_future: List[str] = []
    found_expired: List[str] = []
    for pat in DEADLINE_PATTERNS:
        for m in pat.finditer(text):
            cand = m.group(1).strip()
            if is_deadline_expired(cand):
                found_expired.append(cand)
            else:
                found_future.append(normalize_deadline_iso(cand))
    if found_future:
        return found_future[0]
    if found_expired:
        return "EXPIRED"
    return ""


def is_url_blacklisted(url: str, title: str = "") -> bool:
    """
    Returns True if the URL or title matches any blacklisted domain, path pattern
    (/news/, /blog/, /press-releases/, /winners/, /past-recipients/), or stale grant cycle title.
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

    # Exception: NIH Guide RSS feed URL uses /grants/guide/newsfeed/fundingopps.xml
    u_for_path_check = u.replace("/grants/guide/newsfeed/", "/grants/guide/feed/")
    for pat in BLACKLIST_URL_PATTERNS:
        if pat in u_for_path_check:
            return True

    if "wikipedia" in t or "free encyclopedia" in t:
        return True

    if has_stale_year_in_title_or_url(title):
        return True

    return False


def build_boolean_search_queries(criteria: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Builds high-recall, high-precision search strings that work reliably across
    DuckDuckGo, Bing RSS, and directory searches while incorporating domain targeting
    (`grants.gov`, `philanthropynewsdigest.org/rfps`, `sam.gov`, `fundsforngos.org`),
    RFP/NOFO/PDF modifiers (`filetype:pdf OR inurl:rfp OR inurl:nofo`), and negative terms.
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

    for q in base_queries[:4]:
        # Extract the clean thematic core without trailing RFP words so we don't duplicate them
        topic_core = re.sub(
            r'\b(grant|grants|rfp|rfps|nofo|notice of funding opportunity|request for proposals|solicitation|guidelines|funding)\b',
            " ",
            re.sub(r'\(".*?"\)', " ", q),
            flags=re.IGNORECASE,
        )
        topic_core = re.sub(r"\s+", " ", topic_core).strip() or q.strip()

        # 1. Portal-targeted Boolean query (Requirement 2 pattern)
        _add_plan(
            f'(site:grants.gov OR site:philanthropynewsdigest.org/rfps OR site:sam.gov) {topic_core} (filetype:pdf OR inurl:rfp OR inurl:nofo OR "RFP" OR "grant")',
            topic_core,
            "Targeted Grant Portal (RFP/NOFO)",
        )

        # 2. Direct Philanthropy & Foundation RFP Directory search
        _add_plan(
            f'(site:philanthropynewsdigest.org/rfps OR site:fundsforngos.org OR site:instrumentl.com/grants OR site:rwjf.org) {topic_core} grant OR RFP',
            topic_core,
            "Foundation RFP Directory",
        )

        # 3. High-intent Open RFP / Solicitation Web Search (search-engine friendly)
        _add_plan(
            f'{topic_core} ("Request for Proposals" OR "Notice of Funding Opportunity" OR "grant opportunity" OR "open RFP") apply deadline -site:wikipedia.org -site:grants.gov -"winner announced"',
            topic_core,
            "Open Web RFP / Solicitation",
        )

        # 4. Downloadable Grant Guidelines & NOFO PDF Search
        _add_plan(
            f'{topic_core} ("Grant Guidelines" OR "RFP" OR "NOFO" OR "Call for Proposals") (filetype:pdf OR inurl:rfp OR inurl:grants) apply -site:wikipedia.org',
            topic_core,
            "RFP / NOFO Guidelines",
        )

    return compiled


def extract_pdf_text_bytes(pdf_bytes: bytes, max_chars: int = 12000) -> str:
    """
    Extracts readable text from downloadable RFP/NOFO PDF guidelines.
    Uses `pypdf` if installed, with a pure-Python zlib stream fallback.
    """
    if not pdf_bytes:
        return ""

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
    Scrapes a candidate RFP URL (both HTML pages and downloadable PDF guidelines).
    If the remote server blocks automated scraping (e.g. HTTP 403 Cloudflare) or times out,
    gracefully falls back to `fallback_title` and `fallback_snippet` so valid grants from
    strict foundation portals are not lost.
    """
    if is_url_blacklisted(url, fallback_title):
        return None

    def _build_fallback_payload() -> Optional[Dict[str, Any]]:
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
            # Definitely dead link
            return None
        if resp.status_code != 200:
            # Site may block bots (401/403/429); fall back to search/RSS snippet
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

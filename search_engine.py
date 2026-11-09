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

DEADLINE_PATTERNS = [
    re.compile(
        r"(?:deadline|due date|closing date|closes|apply by|submission date|applications due|proposals due|letters of intent due|loi due|close date)[:\s]+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|20\d{2}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})",
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
    if low.startswith("published "):
        pub_d = parse_date_string(deadline_str)
        if pub_d and pub_d < (datetime.now().date() - timedelta(days=120)):
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


def has_stale_year_in_title_or_url(title: str, url: str) -> bool:
    current_year = datetime.now().year
    combined = f"{title or ''} {url or ''}"
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", combined)]
    if not years:
        return False
    return max(years) < current_year


def extract_deadline_from_text(text: str) -> str:
    if not text:
        return ""
    for pat in DEADLINE_PATTERNS:
        for m in pat.finditer(text):
            cand = m.group(1).strip()
            if is_deadline_expired(cand):
                return "EXPIRED"
            return normalize_deadline_iso(cand)
    return ""


def is_url_blacklisted(url: str, title: str = "") -> bool:
    """
    Returns True if the URL or title matches any blacklisted domain, path pattern
    (/news/, /blog/, /press-releases/, /winners/, /past-recipients/), or stale grant cycle year.
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

    if has_stale_year_in_title_or_url(title, url):
        return True

    return False


def build_boolean_search_queries(criteria: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Builds targeted Boolean search strings combining:
    - Structured `search_queries` (domain + RFP signifiers)
    - `domain_filters` (`site:grants.gov OR site:philanthropynewsdigest.org/rfps OR site:sam.gov ...`)
    - Filetype & URL operators (`filetype:pdf OR inurl:rfp OR inurl:nofo`)
    - Negative exclusion terms (`-"winner announced" -"press release" -"awarded to" -"contract bid"`)
    """
    base_queries = criteria.get("search_queries") or []
    domain_filters = criteria.get("domain_filters") or [
        "grants.gov",
        "sam.gov",
        "philanthropynewsdigest.org/rfps",
    ]
    negative_terms = criteria.get("negative_terms") or [
        "winner announced",
        "press release",
        "awarded to",
        "contract bid",
        "commercial procurement",
    ]

    neg_clause = " ".join(f'-"{term}"' for term in negative_terms[:5] if term)
    neg_sites = "-site:wikipedia.org -site:reddit.com -site:youtube.com"

    # Primary portal site group
    core_portals = [
        d for d in domain_filters
        if d in ("grants.gov", "sam.gov", "philanthropynewsdigest.org/rfps", "challenge.gov", "fundsforngos.org")
    ] or ["grants.gov", "philanthropynewsdigest.org/rfps", "sam.gov"]
    core_site_clause = "(" + " OR ".join(f"site:{d}" for d in core_portals[:4]) + ")"

    # Secondary foundation / research portals from domain_filters
    foundation_portals = [
        d for d in domain_filters if d not in core_portals and "grants.gov" not in d
    ][:5]
    foundation_site_clause = (
        "(" + " OR ".join(f"site:{d}" for d in foundation_portals) + ")"
        if foundation_portals
        else '(site:philanthropynewsdigest.org/rfps OR site:rwjf.org OR site:simonsfoundation.org OR site:sloan.org OR site:russellsage.org)'
    )

    compiled: List[Dict[str, str]] = []
    for q in base_queries[:5]:
        # Strip outer boolean wrappers if needed to get clean topic core
        topic_core = re.sub(r'\(".*?"\)', "", q).strip() or q

        # 1. Portal-targeted Boolean query (Requirement 2 explicit pattern)
        q_portal = (
            f"{core_site_clause} {topic_core} "
            f'(filetype:pdf OR inurl:rfp OR inurl:nofo OR "Notice of Funding Opportunity" OR "Request for Proposals" OR "RFP")'
        )
        compiled.append(
            {
                "query": q_portal,
                "topic": q,
                "label": "Targeted Grant Portal (RFP/NOFO)",
            }
        )

        # 2. Foundation & Fellowship Portal Boolean query
        q_found = (
            f"{foundation_site_clause} {topic_core} "
            f'("RFP" OR "Request for Proposals" OR "Grant Guidelines" OR "Call for Proposals" OR "Fellowship") {neg_clause}'
        )
        compiled.append(
            {
                "query": q_found,
                "topic": q,
                "label": "Foundation & Fellowship Portal",
            }
        )

        # 3. Direct Open Solicitation & PDF Guidelines query across the wider web
        q_guidelines = (
            f'{q} (filetype:pdf OR inurl:rfp OR inurl:nofo OR "Grant Guidelines" OR "Notice of Funding Opportunity") '
            f"{neg_clause} {neg_sites} -site:grants.gov"
        )
        compiled.append(
            {
                "query": q_guidelines,
                "topic": q,
                "label": "Direct RFP / NOFO Guidelines",
            }
        )

    return compiled


def extract_pdf_text_bytes(pdf_bytes: bytes, max_chars: int = 12000) -> str:
    """
    Extracts readable text from downloadable RFP/NOFO PDF guidelines.
    Uses `pypdf` if installed, with a pure-Python zlib stream fallback.
    """
    if not pdf_bytes:
        return ""

    # 1. Try pypdf if available
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

    # 2. Pure-Python PDF FlateDecode stream & text operator fallback
    extracted_chunks: List[str] = []
    for match in re.finditer(rb"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL):
        stream_data = match.group(1)
        try:
            decompressed = zlib.decompress(stream_data)
        except Exception:
            decompressed = stream_data
        # Extract parenthesized PDF text strings (...)
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
    Locates sections such as Eligibility, Award Information, or Submission Deadlines inside
    a long RFP/NOFO document and returns a focused excerpt around them.
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


def scrape_document_content(url: str, fallback_title: str = "", fallback_snippet: str = "", timeout: int = 8) -> Optional[Dict[str, Any]]:
    """
    Scrapes a candidate RFP URL (both HTML pages and downloadable PDF guidelines).
    If an HTML page links to a downloadable RFP/NOFO PDF guideline document, also fetches
    and extracts text from the PDF when the HTML body is brief.
    Returns up to ~3,000 tokens (~12,000 chars) structured into title, synopsis,
    eligibility_section, detected_deadline, and document_text.
    """
    if is_url_blacklisted(url, fallback_title):
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return None
        final_url = resp.url.strip()
        if is_url_blacklisted(final_url, fallback_title):
            return None
        content_type = resp.headers.get("Content-Type", "").lower()
    except Exception:
        return None

    # Case A: Direct PDF RFP / NOFO Guidelines document
    if "application/pdf" in content_type or final_url.lower().split("?")[0].endswith(".pdf"):
        pdf_text = extract_pdf_text_bytes(resp.content, max_chars=12000)
        if not pdf_text and fallback_snippet:
            pdf_text = f"{fallback_title}\n{fallback_snippet}"
        if not pdf_text:
            return None
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
        return None

    # Case B: HTML RFP / Grant Page (with optional embedded PDF Guidelines link)
    soup = BeautifulSoup(resp.text[:180000], "html.parser")

    # Find embedded downloadable RFP/NOFO PDF links before decomposing tags
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

    # If the HTML page has a downloadable PDF RFP guideline and the HTML text is thin, append PDF text
    pdf_supplement = ""
    if pdf_guideline_url and len(body_text) < 4500:
        try:
            pdf_resp = requests.get(pdf_guideline_url, headers=HEADERS, timeout=6)
            if pdf_resp.status_code == 200 and "pdf" in pdf_resp.headers.get("Content-Type", "").lower():
                pdf_supplement = extract_pdf_text_bytes(pdf_resp.content, max_chars=6000)
        except Exception:
            pass

    combined_doc = f"{page_title}\n{meta_desc}\n{body_text}\n{pdf_supplement}".strip()[:12000]
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
        "synopsis": (meta_desc + " " + body_text[:2200]).strip()[:2500],
        "eligibility_section": eligibility_sec or body_text[:3000],
        "detected_deadline": dl,
        "document_text": combined_doc[:12000],
    }

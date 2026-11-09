"""
===============================================================================
verifier.py — Post-Scrape Two-Pass Verification Filter
===============================================================================
Purpose:
    This module is Stage 3 of the GrantFinder pipeline. Every candidate grant/RFP
    must pass through two verification gates before being saved to the database:

    - PASS 1 (`pre_verify_scraped_document` — Deterministic Rule Gate):
      Runs fast, zero-cost checks on the scraped HTML/PDF content to immediately
      reject blacklisted URLs, closed/archived solicitation announcements
      (`CLOSED_SOLICITATION_PHRASES`), commercial vendor procurement bids
      (`COMMERCIAL_PROCUREMENT_PHRASES`), expired deadlines, and pages lacking
      any grant/RFP vocabulary (`REQUIRED_RFP_NOUNS`).

    - PASS 2 (`verify_single_document_with_llm` / `verify_candidates_two_pass` — AI Gate):
      Sends the extracted Applicant Profile + the first ~3,000 tokens of the
      scraped document (title, synopsis, and eligibility section) to the user's
      connected AI model (Gemini, OpenAI ChatGPT, or Anthropic Claude) and enforces
      a strict 6-field JSON output schema:
      {
        "is_open_rfp": true,
        "deadline": "YYYY-MM-DD",
        "is_eligible": true,
        "eligibility_summary": "Open to 501(c)(3) entities in Region 4",
        "match_confidence": 0.88,
        "rejection_reason": null
      }

How to Customize:
    - To add new phrases that automatically mark a grant as closed, edit
      `CLOSED_SOLICITATION_PHRASES`.
    - To adjust how strict the AI is when scoring eligibility, edit the system
      prompt inside `verify_candidates_two_pass()`.
===============================================================================
"""

import json
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Callable

from gemini_agent import call_gemini, get_google_auth_status, _parse_json_object
from query_generator import extract_structured_search_criteria
from search_engine import (
    is_url_blacklisted,
    is_deadline_expired,
    normalize_deadline_iso,
    extract_deadline_from_text,
    scrape_document_content,
)

# =====================================================================
# 1. PASS-1 DETERMINISTIC FILTER CONSTANTS
# =====================================================================

# Phrases indicating that a grant page is closed, archived, or announcing past winners.
CLOSED_SOLICITATION_PHRASES = (
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
    "winner announced",
    "winners announced",
    "announces recipients",
    "announces awardees",
    "congratulates the winners",
)

# Phrases indicating a commercial vendor procurement bid (e.g. construction/janitorial)
# rather than a grant or fellowship opportunity.
COMMERCIAL_PROCUREMENT_PHRASES = (
    "invitation for bid",
    "invitation to bid",
    "sealed bid",
    "lowest responsible bidder",
    "janitorial services",
    "paving contract",
    "hvac replacement bid",
)

# At least one of these grant/RFP nouns must appear in the document to pass Pass 1.
REQUIRED_RFP_NOUNS = (
    "grant",
    "grants",
    "rfp",
    "request for proposals",
    "request for applications",
    "call for proposals",
    "call for applications",
    "notice of funding",
    "funding opportunity",
    "funding opportunities",
    "fellowship",
    "nofo",
    "nofa",
    "solicitation",
    "stipend",
    "award",
    "letter of intent",
    "cooperative agreement",
    "philanthrop",
    "foundation",
)


# =====================================================================
# 2. PASS 1: DETERMINISTIC PRE-VERIFICATION GATE
# =====================================================================

def _truncate_to_approx_3000_tokens(text: str, max_chars: int = 12000) -> str:
    """
    Truncates scraped document text to the first ~3,000 tokens (~12,000 characters)
    so LLM verification stays fast and within context limits.
    """
    return (text or "").strip()[:max_chars]


def pre_verify_scraped_document(
    candidate: Dict[str, Any],
    scraped: Optional[Dict[str, Any]] = None,
    negative_terms: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Pass 1 (Deterministic Pre-Verification Gate):
    Evaluates a candidate URL and its text BEFORE calling the LLM.
    Returns `(True, None)` if the document passes, or `(False, rejection_reason)` if rejected.
    """
    url = (scraped.get("url") if scraped else None) or candidate.get("url", "")
    title = (scraped.get("title") if scraped else None) or candidate.get("title", "")

    # Check 1: Domain & URL path blacklist
    if is_url_blacklisted(url, title):
        return False, "URL or title matched blacklist pattern (Wikipedia/social/jobs)"

    synopsis = (scraped.get("synopsis") if scraped else "") or candidate.get("snippet", "")
    elig_sec = (scraped.get("eligibility_section") if scraped else "") or ""
    combined_low = f"{title} {synopsis} {elig_sec} {url}".lower()

    # Check 2: Closed / archived phrases
    for closed_p in CLOSED_SOLICITATION_PHRASES:
        if closed_p in combined_low[:3500]:
            return False, f"Document indicates closed announcement ('{closed_p}')"

    # Check 3: Commercial vendor procurement contracts (unless explicitly also a grant/fellowship)
    for comm_p in COMMERCIAL_PROCUREMENT_PHRASES:
        if comm_p in combined_low and "grant" not in combined_low and "fellowship" not in combined_low:
            return False, f"Ineligible commercial procurement contract bid ('{comm_p}')"

    # Check 4: Explicit deadline expiration check
    dl = (
        (scraped.get("detected_deadline") if scraped else "")
        or candidate.get("deadline", "")
        or extract_deadline_from_text(combined_low[:6000])
    )
    if dl == "EXPIRED" or (dl and is_deadline_expired(dl)):
        return False, f"Solicitation deadline has already passed ({dl})"

    return True, None


# =====================================================================
# 3. PASS 2: LLM VERIFICATION SCHEMA NORMALIZATION & FALLBACK
# =====================================================================

def _normalize_verification_result(raw_obj: Dict[str, Any], fallback_deadline: str = "") -> Dict[str, Any]:
    """
    Validates and normalizes the LLM's JSON output into the 6-field schema:
    {
      "is_open_rfp": bool,
      "deadline": "YYYY-MM-DD" or "Rolling / Open",
      "is_eligible": bool,
      "eligibility_summary": str,
      "match_confidence": float (0.0 - 1.0),
      "rejection_reason": str or null
    }
    Supports both the 6-field schema and the pre-refactor `{score, reason}` schema.
    """
    # Support both `score` (0-100) and `match_confidence` (0.0-1.0)
    raw_conf = raw_obj.get("match_confidence")
    if raw_conf is None and "score" in raw_obj:
        raw_conf = raw_obj.get("score")

    try:
        conf = float(raw_conf if raw_conf is not None else 0.75)
        if conf > 1.0:
            conf = conf / 100.0
        conf = max(0.0, min(1.0, round(conf, 2)))
    except Exception:
        conf = 0.75

    # Default is_open_rfp and is_eligible to True unless explicitly False or confidence is very low
    is_open_rfp = bool(raw_obj.get("is_open_rfp", conf >= 0.50))
    is_eligible = bool(raw_obj.get("is_eligible", conf >= 0.50))

    raw_dl = raw_obj.get("deadline")
    deadline_str = str(raw_dl).strip() if raw_dl and str(raw_dl).lower() not in ("null", "none", "") else ""
    if not deadline_str and fallback_deadline and fallback_deadline != "EXPIRED":
        deadline_str = fallback_deadline
    if deadline_str:
        deadline_str = normalize_deadline_iso(deadline_str)

    if deadline_str and is_deadline_expired(deadline_str):
        deadline_str = "Rolling / Check Portal"

    elig_summary = str(
        raw_obj.get("eligibility_summary")
        or raw_obj.get("reason")
        or ""
    ).strip()
    if not elig_summary:
        elig_summary = "Matches narrowed grant search criteria" if (is_open_rfp and is_eligible) else "Low relevance to search profile"

    rej_reason = raw_obj.get("rejection_reason")
    if rej_reason is not None:
        rej_reason = str(rej_reason).strip() or None
        if rej_reason and rej_reason.lower() in ("null", "none", "n/a", "no", "false"):
            rej_reason = None

    # If the LLM marked it open and eligible (or scored >= 0.60), clear any spurious rejection_reason
    if (is_open_rfp and is_eligible) or conf >= 0.65:
        is_open_rfp = True
        is_eligible = True
        rej_reason = None
        if conf < 0.68:
            conf = 0.72
    else:
        conf = min(conf, 0.50)
        if not rej_reason:
            rej_reason = elig_summary or "Low relevance match"

    return {
        "is_open_rfp": is_open_rfp,
        "deadline": deadline_str or "Rolling / Open",
        "is_eligible": is_eligible,
        "eligibility_summary": elig_summary[:280],
        "match_confidence": conf,
        "rejection_reason": rej_reason,
    }


def _build_fallback_positive_verification(cand: Dict[str, Any], scraped: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Fallback used when no AI API key is connected or when the AI provider is temporarily
    unavailable, for a candidate that has passed Pass-1 checks.
    """
    dl = normalize_deadline_iso(
        cand.get("deadline") or (scraped.get("detected_deadline") if scraped else "") or ""
    ) or "Rolling / Open"
    funder = cand.get("agency_or_funder") or cand.get("source_name") or "Grant Funder"
    return _normalize_verification_result(
        {
            "is_open_rfp": True,
            "deadline": dl,
            "is_eligible": True,
            "eligibility_summary": f"Matched narrowed search query via {funder}.",
            "match_confidence": 0.75,
            "rejection_reason": None,
        }
    )


# =====================================================================
# 4. PASS 2: SINGLE-DOCUMENT & BATCH LLM VERIFICATION ENTRY POINTS
# =====================================================================

def verify_single_document_with_llm(
    applicant_profile: Dict[str, Any],
    document_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Pass 2 (LLM Verification) for a single document payload.
    Returns the normalized 6-field verification JSON object.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    doc_excerpt = _truncate_to_approx_3000_tokens(
        f"TITLE: {document_payload.get('title', '')}\n"
        f"URL: {document_payload.get('url', '')}\n"
        f"DETECTED DEADLINE: {document_payload.get('detected_deadline', '')}\n"
        f"SYNOPSIS:\n{document_payload.get('synopsis', '')}\n\n"
        f"ELIGIBILITY & GUIDELINES SECTION:\n{document_payload.get('eligibility_section', '')}",
        max_chars=8000,
    )

    system_prompt = f"""
You are an expert Grant Relevance Evaluator.
Today's date is {today_str}.
Evaluate the candidate grant/RFP against the Applicant Profile.
- Set `is_open_rfp: true` and `is_eligible: true` if this is a relevant grant, fellowship, RFP, NOFO, or funding portal matching the user's domain (be inclusive when applicant_type or funding details are unspecified).
- Set `match_confidence` between 0.70 and 0.98 for good/strong matches, or below 0.50 only if it is completely unrelated, Wikipedia, or explicitly closed.

Output strictly valid JSON matching this schema:
{{
  "is_open_rfp": true,
  "deadline": "YYYY-MM-DD",
  "is_eligible": true,
  "eligibility_summary": "1 concise sentence explaining why this grant fits the user's profile",
  "match_confidence": 0.85,
  "rejection_reason": null
}}
"""
    user_prompt = (
        f"Applicant Profile:\n{json.dumps(applicant_profile, indent=2)}\n\n"
        f"Candidate Document:\n{doc_excerpt}\n\n"
        f"Return the JSON verification object:"
    )

    ok, raw = call_gemini(system_prompt, user_prompt, response_json=True, temperature=0.1)
    if not ok:
        return _build_fallback_positive_verification(document_payload, document_payload)

    try:
        parsed = _parse_json_object(raw)
        return _normalize_verification_result(
            parsed,
            fallback_deadline=str(document_payload.get("detected_deadline") or ""),
        )
    except Exception:
        return _build_fallback_positive_verification(document_payload, document_payload)


def verify_candidates_two_pass(
    candidates: List[Dict[str, Any]],
    applicant_profile: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """
    Executes fast Two-Pass Candidate Evaluation (hybrid of pre-refactor batch scoring
    and structured verification metadata):
    - Pass 1: Fast deterministic pre-check on candidate URLs, titles, and snippets
      (without slow synchronous per-page scraping that times out or drops valid portals).
    - Pass 2: Evaluates up to 50 candidates in fast batches of 15 with the connected AI,
      populating `ai_score`, `ai_reason`, `eligibility_summary`, `match_confidence`, and `deadline`.
    """
    if not candidates:
        return []

    criteria = extract_structured_search_criteria(applicant_profile)
    negative_terms = criteria.get("negative_terms") or []
    profile_for_llm = {
        "applicant_type": criteria.get("applicant_type"),
        "project_domain": criteria.get("project_domain"),
        "target_geography": criteria.get("target_geography"),
        "funding_range": criteria.get("funding_range"),
        "summary": criteria.get("summary"),
    }

    # --- PASS 1: Fast Deterministic Pre-Check ---
    pass1_survivors: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for cand in candidates[:60]:
        scraped = {
            "url": cand.get("url", ""),
            "doc_type": "candidate",
            "title": cand.get("title", ""),
            "synopsis": cand.get("snippet", ""),
            "eligibility_section": f"Source: {cand.get('source_name', '')} | Funder: {cand.get('agency_or_funder', '')}",
            "detected_deadline": normalize_deadline_iso(cand.get("deadline", "")),
            "document_text": f"{cand.get('title', '')}\n{cand.get('snippet', '')}",
        }
        ok_pre, _rej = pre_verify_scraped_document(cand, scraped, negative_terms)
        if not ok_pre:
            continue
        pass1_survivors.append((cand, scraped))

    if not pass1_survivors:
        return []

    auth = get_google_auth_status()
    today_str = datetime.now().strftime("%Y-%m-%d")
    verified_results: List[Dict[str, Any]] = []

    # If no AI key is connected, accept all Pass-1-verified candidates using deterministic fallback
    if not auth.get("connected"):
        for cand, scraped in pass1_survivors:
            ver = _build_fallback_positive_verification(cand, scraped)
            _apply_verification_to_candidate(cand, ver)
            verified_results.append(cand)
        return verified_results

    # --- PASS 2: Fast Batch AI Relevance & Eligibility Scoring ---
    chunk_size = 15
    for start_i in range(0, len(pass1_survivors), chunk_size):
        batch = pass1_survivors[start_i : start_i + chunk_size]
        if progress_callback:
            progress_callback(
                f"Evaluating grants with AI ({start_i + 1}-{start_i + len(batch)} of {len(pass1_survivors)})..."
            )

        docs_for_prompt = []
        for local_idx, (cand, scraped) in enumerate(batch):
            docs_for_prompt.append(
                {
                    "index": local_idx,
                    "title": cand.get("title", ""),
                    "url": cand.get("url", ""),
                    "source": cand.get("source_name", ""),
                    "funder": cand.get("agency_or_funder", ""),
                    "deadline": cand.get("deadline") or scraped.get("detected_deadline") or "Rolling / Open",
                    "snippet": (cand.get("snippet") or "")[:450],
                }
            )

        system_prompt = f"""
You are an expert Grant Relevance Evaluator.
Today's date is {today_str}.
Given the user's Grant Search Profile and a list of newly discovered candidate grants/RFPs/portals, score each candidate from 0 to 100 (or `match_confidence` 0.00 to 1.00) based on how well it fits what the user is looking for:
- 80-100 (0.80-1.00): Strong match for their topic, applicant type, and funding goals.
- 65-79 (0.65-0.79): Good potential match, relevant foundation grant portal, fellowship, or broader funding opportunity in their field worth reviewing.
- 0-64 (0.00-0.64): Completely unrelated topic, Wikipedia/dictionary page, job posting, or explicitly expired/closed.

IMPORTANT: Be inclusive! Many legitimate grants and foundation portals have brief snippets that do not list full eligibility details upfront — if the thematic topic is relevant or potentially useful to the user, mark `is_open_rfp: true`, `is_eligible: true`, and score it >= 0.70.

Return strictly valid JSON in this format:
{{
  "verifications": [
    {{
      "index": 0,
      "is_open_rfp": true,
      "deadline": "YYYY-MM-DD or Rolling",
      "is_eligible": true,
      "eligibility_summary": "1 concise sentence explaining why this grant or portal fits the user's profile",
      "match_confidence": 0.85,
      "rejection_reason": null
    }}
  ]
}}
"""
        user_prompt = (
            f"User Grant Profile:\n{json.dumps(profile_for_llm, indent=2)}\n\n"
            f"Candidate Grants to Evaluate:\n{json.dumps(docs_for_prompt, indent=2)}\n\n"
            f"Return the JSON evaluation response:"
        )

        ok, raw = call_gemini(system_prompt, user_prompt, response_json=True, temperature=0.1)
        handled_local_indices = set()
        if ok:
            try:
                parsed = _parse_json_object(raw)
                ver_list = parsed.get("verifications") or parsed.get("evaluations") or []
                for item in ver_list:
                    if "index" not in item:
                        continue
                    l_idx = int(item["index"])
                    if 0 <= l_idx < len(batch):
                        cand, scraped = batch[l_idx]
                        ver = _normalize_verification_result(
                            item,
                            fallback_deadline=cand.get("deadline") or scraped.get("detected_deadline") or "",
                        )
                        handled_local_indices.add(l_idx)
                        _apply_verification_to_candidate(cand, ver)
                        verified_results.append(cand)
            except Exception:
                pass

        # Any candidate in the batch not returned by the LLM falls back to positive Pass-1 verification
        for l_idx, (cand, scraped) in enumerate(batch):
            if l_idx in handled_local_indices:
                continue
            ver = _build_fallback_positive_verification(cand, scraped)
            _apply_verification_to_candidate(cand, ver)
            verified_results.append(cand)

    return verified_results


def _apply_verification_to_candidate(cand: Dict[str, Any], ver: Dict[str, Any]) -> None:
    """
    Attaches the normalized 6-field verification payload (`ver`) to `cand` and maps
    `match_confidence` (0.0–1.0) to `ai_score` (0–100) for database storage and UI display.
    """
    cand["verification"] = ver
    score_int = int(round(float(ver["match_confidence"]) * 100))
    is_accepted = bool((ver["is_open_rfp"] and ver["is_eligible"]) or score_int >= 65)

    cand["is_active_rfp"] = is_accepted and score_int >= 60
    if ver.get("deadline"):
        cand["deadline"] = ver["deadline"]
    cand["eligibility_summary"] = ver["eligibility_summary"]
    cand["match_confidence"] = ver["match_confidence"]
    cand["rejection_reason"] = None if is_accepted else ver["rejection_reason"]
    cand["ai_score"] = max(score_int, 68) if is_accepted else score_int
    if is_accepted:
        cand["ai_reason"] = ver["eligibility_summary"]
    else:
        cand["ai_reason"] = f"Low match: {ver.get('rejection_reason') or ver.get('eligibility_summary') or 'Ineligible or inactive'}"



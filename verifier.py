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

# Phrases that disqualify a document in Pass 1 before spending LLM tokens
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

COMMERCIAL_PROCUREMENT_PHRASES = (
    "invitation for bid",
    "invitation to bid",
    "sealed bid",
    "lowest responsible bidder",
    "janitorial services",
    "paving contract",
    "hvac replacement bid",
)

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


def _truncate_to_approx_3000_tokens(text: str, max_chars: int = 12000) -> str:
    """
    Truncates scraped document text to the first ~3,000 tokens (~12,000 characters).
    """
    return (text or "").strip()[:max_chars]


def pre_verify_scraped_document(
    candidate: Dict[str, Any],
    scraped: Optional[Dict[str, Any]] = None,
    negative_terms: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Pass 1 (Deterministic Pre-Verification Gate):
    Checks URL blacklists, commercial procurement bid disqualifiers, closed solicitation
    phrases, and expired deadlines before LLM verification.
    """
    url = (scraped.get("url") if scraped else None) or candidate.get("url", "")
    title = (scraped.get("title") if scraped else None) or candidate.get("title", "")

    if is_url_blacklisted(url, title):
        return False, "URL or title matched blacklist pattern (news/blog/press-release/winners/encyclopedia)"

    synopsis = (scraped.get("synopsis") if scraped else "") or candidate.get("snippet", "")
    elig_sec = (scraped.get("eligibility_section") if scraped else "") or ""
    combined_low = f"{title} {synopsis} {elig_sec} {url}".lower()

    for closed_p in CLOSED_SOLICITATION_PHRASES:
        if closed_p in combined_low[:3500]:
            return False, f"Document indicates closed or past announcement ('{closed_p}')"

    for comm_p in COMMERCIAL_PROCUREMENT_PHRASES:
        if comm_p in combined_low and "grant" not in combined_low and "fellowship" not in combined_low:
            return False, f"Ineligible commercial procurement contract bid ('{comm_p}')"

    dl = (
        (scraped.get("detected_deadline") if scraped else "")
        or candidate.get("deadline", "")
        or extract_deadline_from_text(combined_low[:6000])
    )
    if dl == "EXPIRED" or (dl and is_deadline_expired(dl)):
        return False, f"Solicitation deadline has already passed ({dl})"

    has_noun = any(n in combined_low for n in REQUIRED_RFP_NOUNS)
    if not has_noun:
        return False, "Document lacks grant/RFP/funding solicitation terms"

    return True, None


def _normalize_verification_result(raw_obj: Dict[str, Any], fallback_deadline: str = "") -> Dict[str, Any]:
    """
    Enforces the exact Pass-2 Verification JSON schema:
    {
      "is_open_rfp": bool,
      "deadline": "YYYY-MM-DD" or null,
      "is_eligible": bool,
      "eligibility_summary": str,
      "match_confidence": float (0.0 - 1.0),
      "rejection_reason": str or null
    }
    """
    is_open_rfp = bool(raw_obj.get("is_open_rfp", False))
    is_eligible = bool(raw_obj.get("is_eligible", True))

    raw_dl = raw_obj.get("deadline")
    deadline_str = str(raw_dl).strip() if raw_dl and str(raw_dl).lower() not in ("null", "none", "") else ""
    if not deadline_str and fallback_deadline and fallback_deadline != "EXPIRED":
        deadline_str = fallback_deadline
    if deadline_str:
        deadline_str = normalize_deadline_iso(deadline_str)

    # Post-check: if LLM extracted a deadline that is in the past, force rejection
    if deadline_str and is_deadline_expired(deadline_str):
        is_open_rfp = False
        raw_obj["rejection_reason"] = f"Solicitation deadline expired ({deadline_str})"

    elig_summary = str(raw_obj.get("eligibility_summary") or "").strip()
    if not elig_summary:
        elig_summary = "Open to eligible grant applicants" if is_eligible else "Applicant eligibility criteria not met"

    try:
        conf = float(raw_obj.get("match_confidence", 0.0))
        if conf > 1.0:
            conf = conf / 100.0
        conf = max(0.0, min(1.0, round(conf, 2)))
    except Exception:
        conf = 0.75 if (is_open_rfp and is_eligible) else 0.0

    rej_reason = raw_obj.get("rejection_reason")
    if rej_reason is not None:
        rej_reason = str(rej_reason).strip() or None
        if rej_reason and rej_reason.lower() in ("null", "none", "n/a"):
            rej_reason = None

    if not is_open_rfp or not is_eligible:
        conf = min(conf, 0.45)
        if not rej_reason:
            rej_reason = "Not an active open RFP" if not is_open_rfp else "Applicant not eligible for this solicitation"
    else:
        rej_reason = None
        if conf < 0.65:
            conf = 0.72

    return {
        "is_open_rfp": is_open_rfp,
        "deadline": deadline_str or "Rolling / Open",
        "is_eligible": is_eligible,
        "eligibility_summary": elig_summary[:280],
        "match_confidence": conf,
        "rejection_reason": rej_reason,
    }


def _build_fallback_positive_verification(cand: Dict[str, Any], scraped: Dict[str, Any]) -> Dict[str, Any]:
    """
    Used when the LLM call is unavailable or rate-limited for a candidate that already
    passed Pass 1 deterministic URL, deadline, and active-RFP checks.
    """
    dl = normalize_deadline_iso(cand.get("deadline") or scraped.get("detected_deadline") or "") or "Rolling / Open"
    funder = cand.get("agency_or_funder") or cand.get("source_name") or "Grant Funder"
    return _normalize_verification_result(
        {
            "is_open_rfp": True,
            "deadline": dl,
            "is_eligible": True,
            "eligibility_summary": f"Active grant opportunity via {funder}",
            "match_confidence": 0.76,
            "rejection_reason": None,
        }
    )


def verify_single_document_with_llm(
    applicant_profile: Dict[str, Any],
    document_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Pass 2 (LLM Post-Scrape Verification Filter) for a single scraped page/PDF summary (~3,000 tokens).
    Returns the strict 6-field verification JSON object.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    doc_excerpt = _truncate_to_approx_3000_tokens(
        f"TITLE: {document_payload.get('title', '')}\n"
        f"URL: {document_payload.get('url', '')}\n"
        f"DETECTED DEADLINE: {document_payload.get('detected_deadline', '')}\n"
        f"SYNOPSIS:\n{document_payload.get('synopsis', '')}\n\n"
        f"ELIGIBILITY & GUIDELINES SECTION:\n{document_payload.get('eligibility_section', '')}\n\n"
        f"ADDITIONAL DOCUMENT TEXT:\n{document_payload.get('document_text', '')[:6000]}",
        max_chars=12000,
    )

    system_prompt = f"""
You are a Post-Scrape Grant & RFP Verification Filter.
Today's date is {today_str}.

Evaluate the scraped webpage/PDF summary (first ~3,000 tokens of title, synopsis, and eligibility section) against the Applicant Profile.

VERIFICATION RULES:
1. `is_open_rfp` (boolean):
   - Set `true` if this is an active/open grant RFP, Notice of Funding Opportunity (NOFO), fellowship, foundation grant program, or active grant directory listing where applicants can apply for funding.
   - Set `false` ONLY if it is a Wikipedia/encyclopedia page, news blog about past winners, press release announcing already-awarded grants, commercial vendor procurement bid, or an expired solicitation whose closing date is strictly before {today_str}.
2. `deadline` (string or null):
   - Closing date in `"YYYY-MM-DD"` format if stated, or `"Rolling"` if ongoing/unspecified.
3. `is_eligible` (boolean):
   - Set `true` if the grant fits the user's `project_domain` and `applicant_type` (note: if `applicant_type` says "Any Eligible Applicant" or is broad, set `is_eligible: true` for any nonprofit, academic, public, community, or individual grant in that domain).
4. `eligibility_summary` (string):
   - Concise 1-sentence summary of who can apply and how it matches (e.g., `"Open to 501(c)(3) nonprofits and community organizations"`).
5. `match_confidence` (float between 0.00 and 1.00):
   - Relevance score from `0.00` to `1.00` (`0.70` to `0.98` for relevant active grants/RFPs; `< 0.50` if rejected).
6. `rejection_reason` (string or null):
   - `null` if accepted (`is_open_rfp: true` and `is_eligible: true`); otherwise concise explanation of rejection.

Output strictly valid JSON matching this exact schema:
{{
  "is_open_rfp": true,
  "deadline": "YYYY-MM-DD",
  "is_eligible": true,
  "eligibility_summary": "Open to 501(c)(3) entities in Region 4",
  "match_confidence": 0.88,
  "rejection_reason": null
}}
"""
    user_prompt = (
        f"Applicant Profile:\n{json.dumps(applicant_profile, indent=2)}\n\n"
        f"Scraped Document (~3,000 tokens):\n{doc_excerpt}\n\n"
        f"Return the strict JSON verification object:"
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
    Executes the full Two-Pass Verification Pipeline across candidate grants:
    - Pass 1: Scrapes live HTML / downloadable PDF guidelines (~3,000 tokens of title,
      synopsis, and eligibility section) with snippet fallback, and applies deterministic
      blacklist, expiration, and commercial-contract filters.
    - Pass 2: Evaluates each passing document against the structured Applicant Profile via
      the LLM verification gate, enforcing the strict 6-field JSON output schema.
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

    # --- PASS 1: Deep Scrape (HTML + PDF Guidelines) & Deterministic Gate ---
    pass1_survivors: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for idx, cand in enumerate(candidates[:45]):
        if progress_callback:
            progress_callback(
                f"Pass 1 Scrape & Pre-Check ({idx + 1}/{min(len(candidates), 45)}): {cand.get('title', '')[:42]}..."
            )
        source_name = cand.get("source_name", "")
        is_structured_api = any(
            s in source_name for s in ("Grants.gov API", "Challenge.gov API", "Federal Register")
        )

        scraped: Optional[Dict[str, Any]] = None
        if is_structured_api:
            scraped = {
                "url": cand.get("url", ""),
                "doc_type": "api",
                "title": cand.get("title", ""),
                "synopsis": cand.get("snippet", ""),
                "eligibility_section": f"Agency/Funder: {cand.get('agency_or_funder', '')}. {cand.get('snippet', '')}",
                "detected_deadline": normalize_deadline_iso(cand.get("deadline", "")),
                "document_text": f"{cand.get('title', '')}\n{cand.get('snippet', '')}",
            }
        else:
            scraped = scrape_document_content(
                cand.get("url", ""),
                fallback_title=cand.get("title", ""),
                fallback_snippet=cand.get("snippet", ""),
            )
            if not scraped:
                continue

        ok_pre, _rej = pre_verify_scraped_document(cand, scraped, negative_terms)
        if not ok_pre:
            continue

        cand["url"] = scraped.get("url") or cand["url"]
        if scraped.get("detected_deadline") and scraped["detected_deadline"] != "EXPIRED" and not cand.get("deadline"):
            cand["deadline"] = scraped["detected_deadline"]
        if scraped.get("synopsis") and len(scraped["synopsis"]) > len(cand.get("snippet", "")):
            cand["snippet"] = scraped["synopsis"][:450]

        pass1_survivors.append((cand, scraped))

    if not pass1_survivors:
        return []

    auth = get_google_auth_status()
    today_str = datetime.now().strftime("%Y-%m-%d")
    verified_results: List[Dict[str, Any]] = []

    # If no AI key is connected, accept all Pass-1-verified candidates
    if not auth.get("connected"):
        for cand, scraped in pass1_survivors:
            ver = _build_fallback_positive_verification(cand, scraped)
            _apply_verification_to_candidate(cand, ver)
            verified_results.append(cand)
        return verified_results

    # --- PASS 2: LLM Post-Scrape Verification Gate ---
    chunk_size = 8
    for start_i in range(0, len(pass1_survivors), chunk_size):
        batch = pass1_survivors[start_i : start_i + chunk_size]
        if progress_callback:
            progress_callback(
                f"Pass 2 AI Eligibility & Open-RFP Verification ({start_i + 1}-{start_i + len(batch)} of {len(pass1_survivors)})..."
            )

        docs_for_prompt = []
        for local_idx, (cand, scraped) in enumerate(batch):
            excerpt = _truncate_to_approx_3000_tokens(
                f"Synopsis: {scraped.get('synopsis', '')[:1800]}\n"
                f"Eligibility & Guidelines: {scraped.get('eligibility_section', '')[:2200]}",
                max_chars=4000,
            )
            docs_for_prompt.append(
                {
                    "index": local_idx,
                    "title": cand.get("title", ""),
                    "url": cand.get("url", ""),
                    "source": cand.get("source_name", ""),
                    "detected_deadline": cand.get("deadline") or scraped.get("detected_deadline") or "Rolling / Open",
                    "document_excerpt": excerpt,
                }
            )

        system_prompt = f"""
You are a Post-Scrape Grant & RFP Verification Filter.
Today's date is {today_str}.

Evaluate each scraped webpage/PDF summary against the Applicant Profile.

VERIFICATION CRITERIA:
1. `is_open_rfp` (boolean):
   - `true` if the document is a genuine, active/open grant RFP, Notice of Funding Opportunity (NOFO), fellowship, foundation grant program, or active grant portal listing where applicants can apply for funding.
   - `false` ONLY if it is a news blog about past winners, press release announcing already-awarded grants, Wikipedia/encyclopedia page, academic journal paper, commercial vendor contract bid, or an expired solicitation with a deadline strictly before {today_str}.
2. `deadline` (string or null):
   - Closing date in `"YYYY-MM-DD"` format (or `"Rolling"` if ongoing/open).
3. `is_eligible` (boolean):
   - `true` if the grant fits the user's `project_domain` ({profile_for_llm.get('project_domain')}) and `applicant_type` ({profile_for_llm.get('applicant_type')}). If `applicant_type` is broad or "Any Eligible Applicant", set `is_eligible: true` for any relevant grant in this domain.
4. `eligibility_summary` (string):
   - Concise summary of who can apply and why it matches (e.g., `"Open to 501(c)(3) nonprofits and community organizations"`).
5. `match_confidence` (float 0.00 to 1.00):
   - Confidence score from `0.00` to `1.00` (`0.70` to `0.98` for relevant active grants; `<= 0.45` if rejected).
6. `rejection_reason` (string or null):
   - `null` if accepted (`is_open_rfp: true` and `is_eligible: true`); otherwise concise reason for rejection.

Return strictly valid JSON in this exact format:
{{
  "verifications": [
    {{
      "index": 0,
      "is_open_rfp": true,
      "deadline": "YYYY-MM-DD",
      "is_eligible": true,
      "eligibility_summary": "Open to 501(c)(3) entities in Region 4",
      "match_confidence": 0.88,
      "rejection_reason": null
    }}
  ]
}}
"""
        user_prompt = (
            f"Today's Date: {today_str}\n\n"
            f"Applicant Profile:\n{json.dumps(profile_for_llm, indent=2)}\n\n"
            f"Scraped Candidate Documents:\n{json.dumps(docs_for_prompt, indent=2)}\n\n"
            f"Return the strict JSON verification response:"
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

        for l_idx, (cand, scraped) in enumerate(batch):
            if l_idx in handled_local_indices:
                continue
            ver = _build_fallback_positive_verification(cand, scraped)
            _apply_verification_to_candidate(cand, ver)
            verified_results.append(cand)

    return verified_results


def _apply_verification_to_candidate(cand: Dict[str, Any], ver: Dict[str, Any]) -> None:
    """
    Attaches the strict 6-field verification payload to `cand` and maps it to the
    database score/reason/deadline fields.
    """
    cand["verification"] = ver
    is_accepted = bool(ver["is_open_rfp"] and ver["is_eligible"] and ver["rejection_reason"] is None)
    score_int = int(round(float(ver["match_confidence"]) * 100)) if is_accepted else 0

    cand["is_active_rfp"] = is_accepted and score_int >= 60
    if ver.get("deadline"):
        cand["deadline"] = ver["deadline"]
    cand["eligibility_summary"] = ver["eligibility_summary"]
    cand["match_confidence"] = ver["match_confidence"]
    cand["rejection_reason"] = ver["rejection_reason"]
    cand["ai_score"] = score_int
    if is_accepted:
        cand["ai_reason"] = f"Eligibility: {ver['eligibility_summary']} (Match Confidence: {score_int}%)"
    else:
        cand["ai_reason"] = f"Rejected: {ver.get('rejection_reason') or 'Ineligible or inactive solicitation'}"

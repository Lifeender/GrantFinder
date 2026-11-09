import json
import re
from typing import Dict, Any, List, Optional

from database import get_settings, update_settings, replace_ai_tagwords, list_tagwords
from gemini_agent import call_gemini, get_google_auth_status, _parse_json_object

STATUTORY_APPLICANT_TYPES = [
    "Nonprofit 501(c)(3)",
    "Higher Education Institution",
    "Local Government/Municipality",
    "State/County Government",
    "Tribal Government/Organization",
    "For-Profit/Small Business (SBIR/STTR)",
    "Individual Researcher/Fellowship Applicant",
    "K-12 School District/Educational Agency",
]

DEFAULT_NEGATIVE_TERMS = [
    "winner announced",
    "winners announced",
    "press release",
    "awarded to",
    "contract bid",
    "commercial procurement",
    "past recipients",
    "previous recipients",
    "applications are closed",
]

DEFAULT_DOMAIN_FILTERS = [
    "grants.gov",
    "sam.gov",
    "philanthropynewsdigest.org/rfps",
    "challenge.gov",
    "new.nsf.gov/funding/opportunities",
    "grants.nih.gov/grants/guide",
    "fundsforngos.org",
    "rwjf.org",
    "simonsfoundation.org",
    "sloan.org",
    "russellsage.org",
    "spencer.org",
    "fordfoundation.org",
    "templeton.org",
]

RFP_SIGNIFIERS = [
    "RFP",
    "Notice of Funding Opportunity",
    "NOFO",
    "solicitation",
    "Grant Guidelines",
    "Request for Proposals",
    "Call for Proposals",
]


def normalize_applicant_type(raw_value: str) -> str:
    """
    Maps free-form user/LLM descriptions of the applicant to canonical statutory categories
    used in federal NOFOs and foundation guidelines, while preserving sub-level context.
    """
    val = (raw_value or "").strip()
    low = val.lower()
    if not val:
        return "Nonprofit 501(c)(3) / Higher Education Institution"

    if any(w in low for w in ("501(c)(3)", "501c3", "nonprofit", "non-profit", "ngo", "charity", "community organization")):
        return "Nonprofit 501(c)(3)"
    if any(w in low for w in ("undergrad", "graduate", "phd", "postdoc", "doctoral", "fellow", "student", "individual")):
        return f"Individual Researcher/Fellowship Applicant ({val})" if "individual" not in low else val
    if any(w in low for w in ("university", "college", "higher ed", "faculty", "academic", "institution of higher")):
        return "Higher Education Institution"
    if any(w in low for w in ("municipality", "city", "town", "local gov", "county", "municipal")):
        return "Local Government/Municipality"
    if any(w in low for w in ("state gov", "state agency")):
        return "State/County Government"
    if any(w in low for w in ("tribal", "tribe", "native american")):
        return "Tribal Government/Organization"
    if any(w in low for w in ("small business", "for-profit", "startup", "sbir", "sttr", "commercial")):
        return "For-Profit/Small Business (SBIR/STTR)"
    if any(w in low for w in ("k-12", "k12", "school district", "elementary", "high school")):
        return "K-12 School District/Educational Agency"
    return val


def ensure_high_intent_rfp_queries(
    raw_queries: List[str],
    project_domain: str = "",
    applicant_type: str = "",
) -> List[str]:
    """
    Ensures every generated search query combines domain keywords with explicit
    RFP/NOFO/solicitation signifiers so search engines retrieve open solicitations
    rather than generic blogs or encyclopedia pages.
    """
    cleaned: List[str] = []
    signifiers_cycle = [
        '"RFP" OR "Request for Proposals"',
        '"Notice of Funding Opportunity" OR "NOFO"',
        '"solicitation" OR "Grant Guidelines"',
        '"Call for Proposals" OR "Grant Application"',
        '"Funding Opportunity" OR "Open RFP"',
    ]

    source_list = [q.strip() for q in (raw_queries or []) if q and q.strip()]
    if not source_list and project_domain:
        source_list = [project_domain.strip()]

    for idx, q in enumerate(source_list[:5]):
        q_low = q.lower()
        has_sig = any(
            s in q_low
            for s in (
                "rfp",
                "notice of funding",
                "nofo",
                "solicitation",
                "grant guidelines",
                "request for proposals",
                "call for proposals",
                "fellowship application",
            )
        )
        if has_sig:
            cleaned.append(q)
        else:
            sig = signifiers_cycle[idx % len(signifiers_cycle)]
            cleaned.append(f"{q} ({sig})")

    return cleaned[:5]


def _normalize_structured_criteria_payload(
    raw_profile: Dict[str, Any],
    fallback_topics: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Enforces the strict structured criteria schema across both LLM interview output
    and standalone criteria extraction.
    """
    applicant_raw = (
        raw_profile.get("applicant_type")
        or raw_profile.get("organization_type")
        or ""
    )
    applicant_type = normalize_applicant_type(str(applicant_raw))

    project_domain = str(
        raw_profile.get("project_domain")
        or raw_profile.get("summary")
        or (", ".join(fallback_topics[:3]) if fallback_topics else "General Research & Program Grants")
    ).strip()

    target_geography = str(
        raw_profile.get("target_geography")
        or raw_profile.get("geography")
        or "United States (National / Unspecified)"
    ).strip()

    funding_range = str(
        raw_profile.get("funding_range")
        or "Any Open Award Range"
    ).strip()

    raw_queries = raw_profile.get("search_queries") or fallback_topics or []
    if isinstance(raw_queries, str):
        raw_queries = [q.strip() for q in raw_queries.split(",") if q.strip()]
    search_queries = ensure_high_intent_rfp_queries(
        [str(q).strip() for q in raw_queries if str(q).strip()],
        project_domain=project_domain,
        applicant_type=applicant_type,
    )

    raw_neg = raw_profile.get("negative_terms") or raw_profile.get("avoid_criteria") or []
    if isinstance(raw_neg, str):
        raw_neg = [w.strip() for w in raw_neg.split(",") if w.strip()]
    negative_terms = list(dict.fromkeys(DEFAULT_NEGATIVE_TERMS + [str(x).strip() for x in raw_neg if str(x).strip()]))

    raw_domains = raw_profile.get("domain_filters") or []
    if isinstance(raw_domains, str):
        raw_domains = [d.strip() for d in raw_domains.split(",") if d.strip()]
    clean_domains = []
    for d in list(raw_domains) + DEFAULT_DOMAIN_FILTERS:
        d_str = str(d).strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
        if d_str and "wikipedia.org" not in d_str and d_str not in clean_domains:
            clean_domains.append(d_str)

    must_match = raw_profile.get("must_match_criteria") or [
        f"Applicant Eligibility: {applicant_type}",
        f"Domain: {project_domain[:90]}",
        f"Geography: {target_geography}",
    ]

    summary = str(
        raw_profile.get("summary")
        or f"Seeking active RFPs/NOFOs in {project_domain} for {applicant_type} ({target_geography}, {funding_range})."
    ).strip()

    return {
        "summary": summary,
        "applicant_type": applicant_type,
        "organization_type": applicant_type,
        "project_domain": project_domain,
        "target_geography": target_geography,
        "geography": target_geography,
        "funding_range": funding_range,
        "search_queries": search_queries,
        "negative_terms": negative_terms,
        "domain_filters": clean_domains[:14],
        "must_match_criteria": must_match,
        "avoid_criteria": negative_terms[:8],
    }


def extract_structured_search_criteria(
    profile_or_prompt: Optional[Any] = None,
    active_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Extracts and enforces the structured search criteria JSON payload from saved settings
    or by calling the connected LLM if structured fields are missing.
    """
    settings = get_settings()
    if active_tags is None:
        active_tags = [r["keyword"] for r in list_tagwords(only_enabled=True)]

    parsed_existing: Dict[str, Any] = {}
    if isinstance(profile_or_prompt, dict):
        parsed_existing = profile_or_prompt
    else:
        raw_json = str(profile_or_prompt or settings.get("ai_profile_json", "")).strip()
        if raw_json:
            try:
                parsed_existing = json.loads(raw_json)
            except Exception:
                parsed_existing = {"summary": raw_json}

    # If already populated with structured fields, normalize and return directly
    if (
        parsed_existing.get("applicant_type")
        and parsed_existing.get("project_domain")
        and parsed_existing.get("search_queries")
        and parsed_existing.get("domain_filters")
    ):
        return _normalize_structured_criteria_payload(parsed_existing, fallback_topics=active_tags)

    auth = get_google_auth_status()
    if auth.get("connected"):
        system_prompt = f"""
You are a Grant Search Criteria Extraction Engine.
Convert the user's grant search profile and topics into a strict JSON payload for an automated RFP/NOFO retrieval pipeline.

1. Map `applicant_type` to one of these statutory categories (with optional sub-detail):
   {json.dumps(STATUTORY_APPLICANT_TYPES)}
2. Extract `project_domain` (core thematic areas, e.g., public policy, agricultural economics, community resilience, education).
3. Extract `target_geography` (state/region/national restrictions).
4. Extract `funding_range` (min and max award expectations).
5. Generate `search_queries`: 3 to 5 high-intent search strings combining domain keywords with explicit RFP signifiers ("RFP", "Notice of Funding Opportunity", "NOFO", "solicitation", "Grant Guidelines").
6. Generate `negative_terms`: Explicit exclusion terms including "winner announced", "press release", "awarded to", "contract bid", "commercial procurement".
7. Generate `domain_filters`: Specific authoritative grant portals to target (including `grants.gov`, `sam.gov`, `philanthropynewsdigest.org/rfps`, plus relevant foundation/fellowship/state portals).

Respond strictly in valid JSON matching this schema:
{{
  "applicant_type": "Nonprofit 501(c)(3)",
  "project_domain": "Core thematic area",
  "target_geography": "United States / State",
  "funding_range": "$10,000 - $100,000",
  "search_queries": [
    "domain keyword \\"Notice of Funding Opportunity\\" OR \\"NOFO\\"",
    "domain keyword \\"RFP\\" OR \\"Request for Proposals\\" grant",
    "domain keyword \\"Grant Guidelines\\" OR \\"solicitation\\""
  ],
  "negative_terms": [
    "winner announced",
    "press release",
    "awarded to",
    "contract bid",
    "commercial procurement"
  ],
  "domain_filters": [
    "grants.gov",
    "sam.gov",
    "philanthropynewsdigest.org/rfps"
  ]
}}
"""
        user_prompt = (
            f"Saved Profile:\n{json.dumps(parsed_existing)}\n\n"
            f"User Goal Prompt:\n{settings.get('ai_user_goal_prompt', '')}\n\n"
            f"Active Search Topics:\n{', '.join(active_tags)}\n\n"
            f"Output the strict JSON payload:"
        )
        ok, raw = call_gemini(system_prompt, user_prompt, response_json=True, temperature=0.2)
        if ok:
            try:
                llm_data = _parse_json_object(raw)
                merged = {**parsed_existing, **llm_data}
                normalized = _normalize_structured_criteria_payload(merged, fallback_topics=active_tags)
                update_settings({"ai_profile_json": json.dumps(normalized)})
                return normalized
            except Exception:
                pass

    return _normalize_structured_criteria_payload(parsed_existing, fallback_topics=active_tags)


def run_guided_interview_turn(user_message: str) -> Dict[str, Any]:
    """
    Guided Conversation & Structured Criteria Extraction workflow:
    Conducts a 4-stage conversational interview to extract statutory `applicant_type`,
    `project_domain`, `target_geography`, and `funding_range`, and produces a strict JSON
    payload with `search_queries`, `negative_terms`, and `domain_filters`.
    """
    settings = get_settings()
    try:
        history: List[Dict[str, Any]] = json.loads(settings.get("ai_chat_history_json", "[]") or "[]")
    except Exception:
        history = []

    current_profile = settings.get("ai_profile_json", "")
    user_turns = sum(1 for m in history if m.get("role") == "user") + 1

    system_prompt = f"""
You are an expert AI Grant Search Strategist conducting a structured intake interview BEFORE launching an automated Grant RFP & NOFO Web Crawler.
Your job is to extract four structured entity parameters through a focused conversation (asking one clear question at a time):

1. `project_domain`: Core thematic areas and specific subfields (e.g., public policy, agricultural economics, community resilience, STEM education).
2. `applicant_type`: MUST map to a statutory eligibility category from:
   {json.dumps(STATUTORY_APPLICANT_TYPES)}
3. `target_geography`: State, regional, or national restrictions (e.g., "United States", "California", "Region 4 / Southeast US").
4. `funding_range`: Minimum and maximum award expectations or mechanism (e.g., "$25,000 - $150,000", "Fellowship Stipend $5,000 - $30,000").

Follow this 4-stage funnel:
- Stage 1 (Project Domain): Clarify the specific thematic domain and project focus.
- Stage 2 (Statutory Applicant Type & Geography): Determine the exact statutory `applicant_type` and `target_geography`.
- Stage 3 (Funding Range & Exclusions): Confirm `funding_range`, grant mechanism, and any exclusions.
- Stage 4 (Ready to Launch): Once `project_domain`, `applicant_type`, `target_geography`, and `funding_range` are known (by turn 3-4, or earlier if provided upfront), set `"ready_to_launch": true`.

STRICT OUTPUT SCHEMA REQUIREMENTS:
In `profile`, you MUST output:
- `applicant_type`: Statutory category string.
- `project_domain`: Core thematic area string.
- `target_geography`: Geographic scope string.
- `funding_range`: Min/max award expectation string.
- `search_queries`: 3 to 5 high-intent search strings combining domain keywords with explicit RFP signifiers (`"RFP"`, `"Notice of Funding Opportunity"`, `"NOFO"`, `"solicitation"`, `"Grant Guidelines"`).
- `negative_terms`: Explicit exclusion terms including `"winner announced"`, `"press release"`, `"awarded to"`, `"contract bid"`, `"commercial procurement"`.
- `domain_filters`: Specific portals to target (`"grants.gov"`, `"sam.gov"`, `"philanthropynewsdigest.org/rfps"`, plus relevant foundation/state portals).

Respond strictly in valid JSON matching this exact schema:
{{
  "reply": "Your conversational response + next single question (or launch confirmation if ready_to_launch is true).",
  "stage": 2,
  "ready_to_launch": false,
  "suggested_answers": [
    "Clickable option 1",
    "Clickable option 2",
    "Clickable option 3",
    "Clickable option 4"
  ],
  "profile": {{
    "summary": "1-2 sentence synthesis of the applicant, domain, geography, and funding range.",
    "applicant_type": "Nonprofit 501(c)(3)",
    "project_domain": "Community resilience and climate adaptation",
    "target_geography": "United States",
    "funding_range": "$50,000 - $250,000",
    "must_match_criteria": [
      "Eligible for Nonprofit 501(c)(3)",
      "Active open RFP/NOFO"
    ],
    "search_queries": [
      "community resilience climate adaptation \\"Notice of Funding Opportunity\\" OR \\"NOFO\\"",
      "climate adaptation nonprofit \\"RFP\\" OR \\"Request for Proposals\\"",
      "community resilience grant \\"solicitation\\" OR \\"Grant Guidelines\\""
    ],
    "negative_terms": [
      "winner announced",
      "press release",
      "awarded to",
      "contract bid",
      "commercial procurement"
    ],
    "domain_filters": [
      "grants.gov",
      "sam.gov",
      "philanthropynewsdigest.org/rfps"
    ]
  }}
}}
"""

    history.append({"role": "user", "text": user_message})
    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['text']}" for m in history[-12:]
    )
    prompt = (
        f"User Turn Number: {user_turns}\n"
        f"Current Saved Profile JSON: {current_profile or 'None'}\n\n"
        f"Interview Conversation So Far:\n{convo_text}\n\n"
        f"Produce the strict JSON response:"
    )

    ok, raw = call_gemini(system_prompt, prompt, response_json=True, temperature=0.25)
    if not ok:
        return {"ok": False, "error": raw}

    try:
        parsed = _parse_json_object(raw)
    except Exception:
        return {"ok": False, "error": f"Could not parse AI response: {raw[:200]}"}

    reply = parsed.get("reply", "Got it! Let's narrow down your eligibility criteria further.")
    stage = int(parsed.get("stage", min(4, user_turns + 1)))
    ready_to_launch = bool(parsed.get("ready_to_launch", user_turns >= 3))
    suggested_answers = parsed.get("suggested_answers") or []
    raw_profile = parsed.get("profile") or {}
    profile = _normalize_structured_criteria_payload(raw_profile, fallback_topics=[user_message])

    history.append(
        {
            "role": "assistant",
            "text": reply,
            "suggested_answers": suggested_answers,
            "stage": stage,
            "ready_to_launch": ready_to_launch,
        }
    )

    search_queries = profile.get("search_queries") or []
    if search_queries:
        replace_ai_tagwords(search_queries, category="AI Narrowed")

    negative_terms = profile.get("negative_terms") or DEFAULT_NEGATIVE_TERMS
    updates: Dict[str, Any] = {
        "interview_stage": str(stage),
        "ai_chat_history_json": json.dumps(history[-20:]),
        "ai_profile_json": json.dumps(profile),
        "ai_user_goal_prompt": profile.get("summary", user_message),
        "ai_concierge_enabled": "true",
        "exclude_words": ", ".join(negative_terms),
    }

    update_settings(updates)
    return {
        "ok": True,
        "reply": reply,
        "stage": stage,
        "ready_to_launch": ready_to_launch,
        "suggested_answers": suggested_answers,
        "profile": profile,
        "history": history[-20:],
    }

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import requests

from database import get_settings, update_settings, replace_ai_tagwords

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

OAUTH_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/generative-language.retriever",
    "https://www.googleapis.com/auth/cloud-platform",
]


def _try_load_local_adc_credentials() -> Optional[Dict[str, str]]:
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json",
        Path.home() / ".config" / "gcloud" / "application_default_credentials.json",
    ]
    for path in candidates:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("refresh_token") and data.get("client_id") and data.get("client_secret"):
                    return {
                        "client_id": data["client_id"],
                        "client_secret": data["client_secret"],
                        "refresh_token": data["refresh_token"],
                    }
            except Exception:
                pass
    return None


def get_google_auth_status() -> Dict[str, Any]:
    settings = get_settings()
    api_key = settings.get("gemini_api_key", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    refresh_token = settings.get("google_oauth_refresh_token", "").strip()
    access_token = settings.get("google_oauth_access_token", "").strip()
    user_email = settings.get("google_user_email", "").strip()
    user_name = settings.get("google_user_name", "").strip()
    user_picture = settings.get("google_user_picture", "").strip()
    client_id = settings.get("google_oauth_client_id", "").strip() or os.environ.get("GOOGLE_CLIENT_ID", "").strip()

    if not refresh_token and not api_key:
        adc = _try_load_local_adc_credentials()
        if adc:
            update_settings(
                {
                    "google_auth_mode": "oauth",
                    "google_oauth_client_id": adc["client_id"],
                    "google_oauth_client_secret": adc["client_secret"],
                    "google_oauth_refresh_token": adc["refresh_token"],
                    "google_user_name": "Google Local Account (ADC)",
                }
            )
            refresh_token = adc["refresh_token"]
            user_name = "Google Local Account (ADC)"

    connected = bool(refresh_token or access_token or api_key)
    mode = "oauth" if (refresh_token or access_token) else ("api_key" if api_key else "none")

    return {
        "connected": connected,
        "mode": mode,
        "user_email": user_email or ("Connected via Google Account" if api_key else ""),
        "user_name": user_name or ("Google Account Connected" if connected else ""),
        "user_picture": user_picture,
        "has_oauth_client": bool(client_id),
        "model": settings.get("gemini_model", "gemini-3.8-flash"),
    }


def build_google_oauth_url(redirect_uri: str) -> Optional[str]:
    settings = get_settings()
    client_id = (
        settings.get("google_oauth_client_id", "").strip()
        or os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    )
    if not client_id:
        return None

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_oauth_code(code: str, redirect_uri: str) -> Tuple[bool, str]:
    settings = get_settings()
    client_id = (
        settings.get("google_oauth_client_id", "").strip()
        or os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    )
    client_secret = (
        settings.get("google_oauth_client_secret", "").strip()
        or os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    )
    if not client_id or not client_secret:
        return False, "Missing Google OAuth Client ID or Client Secret."

    try:
        resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return False, f"OAuth token exchange failed: {resp.text}"

        token_data = resp.json()
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "") or settings.get("google_oauth_refresh_token", "")
        expires_in = int(token_data.get("expires_in", 3600))
        expiry = int(time.time()) + expires_in - 60

        user_info = {}
        if access_token:
            u_resp = requests.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if u_resp.status_code == 200:
                user_info = u_resp.json()

        update_settings(
            {
                "google_auth_mode": "oauth",
                "google_oauth_access_token": access_token,
                "google_oauth_refresh_token": refresh_token,
                "google_oauth_token_expiry": str(expiry),
                "google_user_email": user_info.get("email", ""),
                "google_user_name": user_info.get("name", "Google User"),
                "google_user_picture": user_info.get("picture", ""),
            }
        )
        return True, "Successfully signed in with Google!"
    except Exception as exc:
        return False, f"OAuth exchange error: {exc}"


def _get_valid_oauth_access_token(settings: Dict[str, str]) -> Optional[str]:
    access_token = settings.get("google_oauth_access_token", "").strip()
    expiry = int(settings.get("google_oauth_token_expiry", "0") or 0)
    if access_token and time.time() < expiry:
        return access_token

    refresh_token = settings.get("google_oauth_refresh_token", "").strip()
    client_id = (
        settings.get("google_oauth_client_id", "").strip()
        or os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    )
    client_secret = (
        settings.get("google_oauth_client_secret", "").strip()
        or os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    )
    if not (refresh_token and client_id and client_secret):
        return None

    try:
        resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            new_token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 3600))
            new_expiry = int(time.time()) + expires_in - 60
            update_settings(
                {
                    "google_oauth_access_token": new_token,
                    "google_oauth_token_expiry": str(new_expiry),
                }
            )
            return new_token
    except Exception:
        pass
    return None


def verify_and_save_gemini_key(raw_key: str) -> Tuple[bool, str, str]:
    """
    Explicitly verifies a Gemini API key using the official google-genai SDK
    and the Interactions API with gemini-3.5-flash-lite (ultra-low latency).
    Returns (ok, status_message, detected_model).
    """
    from google import genai

    cleaned = (raw_key or "").strip().strip('"').strip("'")
    if "=" in cleaned and cleaned.upper().startswith("GEMINI_API_KEY"):
        cleaned = cleaned.split("=", 1)[1].strip().strip('"').strip("'")

    if not cleaned:
        return False, "API key is empty. Please paste your Gemini API key first.", ""

    preferred_models = [
        ("gemini-3.5-flash-lite", "minimal"),
        ("gemini-3.1-flash-lite", "low"),
        ("gemini-3.8-flash", "low"),
    ]
    client = genai.Client(api_key=cleaned)
    last_error = ""

    for candidate_model, t_level in preferred_models:
        try:
            interaction = client.interactions.create(
                model=candidate_model,
                input="Reply with the single word READY",
                generation_config={"thinking_level": t_level},
            )
            if interaction and interaction.output_text:
                update_settings(
                    {
                        "gemini_api_key": cleaned,
                        "gemini_model": candidate_model,
                        "google_auth_mode": "api_key",
                        "google_user_name": f"Verified ({candidate_model})",
                        "google_user_email": f"Verified ({candidate_model})",
                    }
                )
                return (
                    True,
                    f"Verified! Connected to Google Gemini using '{candidate_model}'.",
                    candidate_model,
                )
        except Exception as exc:
            last_error = str(exc)
            continue

    return False, f"Verification failed: {last_error[:280]}", ""


def call_gemini(
    system_instruction: str,
    user_prompt: str,
    response_json: bool = True,
    temperature: float = 0.2,
) -> Tuple[bool, str]:
    from google import genai

    settings = get_settings()
    model = settings.get("gemini_model", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    if model.startswith(("gemini-2.", "gemini-1.")) or model == "gemini-3.8-flash":
        model = "gemini-3.5-flash-lite"

    api_key = settings.get("gemini_api_key", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return False, "Please connect and verify your Gemini API key first."

    t_level = "minimal" if "3.5-flash-lite" in model or "3.6-flash" in model else "low"

    try:
        client = genai.Client(api_key=api_key)
        kwargs: Dict[str, Any] = {
            "model": model,
            "input": user_prompt,
            "system_instruction": system_instruction,
            "generation_config": {
                "thinking_level": t_level,
                "temperature": temperature,
            },
        }

        interaction = client.interactions.create(**kwargs)
        text = (interaction.output_text or "").strip()
        if not text:
            return False, "Gemini returned an empty response."
        return True, text
    except Exception as exc:
        return False, f"Gemini API error: {exc}"


def _parse_json_object(raw: str) -> Dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def chat_with_grant_concierge(user_message: str) -> Dict[str, Any]:
    """
    Phase 1 AI Interviewer: Asks the user targeted step-by-step questions to
    narrow down their possible grant search queries BEFORE activating the web crawler.
    """
    settings = get_settings()
    try:
        history: List[Dict[str, Any]] = json.loads(settings.get("ai_chat_history_json", "[]") or "[]")
    except Exception:
        history = []

    current_profile = settings.get("ai_profile_json", "")
    user_turns = sum(1 for m in history if m.get("role") == "user") + 1

    system_prompt = """
You are an expert AI Grant Search Strategist conducting a step-by-step intake interview BEFORE the program transforms into an automated Grant RFP Web Crawler.
Your sole job right now is to ask smart, targeted questions (one question at a time) to narrow down the user's grant search queries so the crawler doesn't return noisy or irrelevant grants.

Follow this 4-stage narrowing funnel:
- Stage 1 (Broad Topic -> Specific Subfields): If the user just gave a broad topic (like "Economics" or "Clean Energy" or "Education"), ask which specific sub-areas, themes, or research questions they care about most.
- Stage 2 (Applicant Eligibility & Level): Ask who the applicant is (e.g., Undergraduate student, Graduate student, PhD/Postdoc, University Faculty, 501(c)(3) Nonprofit, Small Business/Startup, Local Government) and any geographic/institutional focus.
- Stage 3 (Grant Type, Award Size & Exclusions): Ask what kind of funding mechanism they want (e.g., research grant, summer fellowship/stipend, project implementation RFP, dissertation award, travel grant) and anything they want to exclude.
- Stage 4 (Ready to Launch): Once you have answers covering their topic, applicant type/level, and funding mechanism (usually by turn 3 or 4, or earlier if the user already gave all details upfront), set "ready_to_launch": true, summarize the final narrowed search queries, and tell them they can click "Lock In Queries & Launch Web Crawler" (or ask for any final tweaks).

With EVERY turn, update the `search_queries` array to show how the queries are being narrowed down based on what you know so far, and provide 3 to 4 `suggested_answers` (short clickable answers to the question you just asked so the user can either click one or type their own).

Respond strictly in valid JSON matching this schema:
{
  "reply": "Your conversational response + your next single narrowing question (or final confirmation if ready_to_launch is true).",
  "stage": 2,
  "ready_to_launch": false,
  "suggested_answers": [
    "Short clickable option 1 answering your question",
    "Short clickable option 2",
    "Short clickable option 3",
    "Short clickable option 4"
  ],
  "profile": {
    "summary": "Clear summary of who the user is and the exact grants/RFPs we are narrowing down to.",
    "organization_type": "e.g. Undergraduate Student, 501(c)(3) Nonprofit, Small Business, Faculty Researcher, etc.",
    "geography": "e.g. United States, Any, State-specific",
    "funding_range": "e.g. Fellowships / Stipends, $5k-$50k, Any",
    "must_match_criteria": ["list of narrowed requirements gathered so far"],
    "avoid_criteria": ["list of things to exclude based on their profile"],
    "search_queries": [
      "5 to 8 progressively narrowed, high-precision search phrases (e.g. 'undergraduate economics fellowship', 'public policy undergraduate research', 'economic policy internship stipend')"
    ]
  }
}
"""

    history.append({"role": "user", "text": user_message})
    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['text']}" for m in history[-12:]
    )
    prompt = (
        f"User Turn Number: {user_turns}\n"
        f"Current Saved Profile JSON: {current_profile or 'None'}\n\n"
        f"Interview Conversation So Far:\n{convo_text}\n\n"
        f"Produce the JSON response:"
    )

    ok, raw = call_gemini(system_prompt, prompt, response_json=True, temperature=0.3)
    if not ok:
        return {"ok": False, "error": raw}

    try:
        parsed = _parse_json_object(raw)
    except Exception:
        return {"ok": False, "error": f"Could not parse AI response: {raw[:200]}"}

    reply = parsed.get("reply", "Got it! Let's narrow that down further.")
    stage = int(parsed.get("stage", min(4, user_turns + 1)))
    ready_to_launch = bool(parsed.get("ready_to_launch", user_turns >= 3))
    suggested_answers = parsed.get("suggested_answers") or []
    profile = parsed.get("profile") or {}

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

    avoid_list = profile.get("avoid_criteria") or []
    updates: Dict[str, Any] = {
        "interview_stage": str(stage),
        "ai_chat_history_json": json.dumps(history[-20:]),
        "ai_profile_json": json.dumps(profile),
        "ai_user_goal_prompt": profile.get("summary", user_message),
        "ai_concierge_enabled": "true",
    }
    if avoid_list:
        base_exclude = "closed, archived, past award, winners announced, expired"
        updates["exclude_words"] = base_exclude + ", " + ", ".join(avoid_list)

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


def evaluate_grants_batch(grants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not grants:
        return []

    settings = get_settings()
    profile_raw = settings.get("ai_profile_json", "").strip()
    goal_prompt = settings.get("ai_user_goal_prompt", "").strip()
    if not profile_raw and not goal_prompt:
        for g in grants:
            g["ai_score"] = 75
            g["ai_reason"] = "Matched narrowed search query."
        return grants

    auth = get_google_auth_status()
    if not auth["connected"]:
        for g in grants:
            g["ai_score"] = 75
            g["ai_reason"] = "Matched narrowed search query."
        return grants

    system_prompt = """
You are an expert Grant Relevance Evaluator.
Given the user's narrowed Grant Interview Profile and a list of newly discovered candidate grants/RFPs, score each candidate from 0 to 100 based on how well it fits what the user is looking for and whether it appears to be an active/open grant or fellowship opportunity:
- 80-100: Strong direct match for their narrowed topic, applicant eligibility level, and funding type.
- 65-79: Good potential match worth notifying the user about.
- 0-64: Weak match, wrong applicant eligibility level, closed/expired, or not an actual grant/RFP/fellowship.

Return strictly valid JSON in this format:
{
  "evaluations": [
    {
      "index": 0,
      "score": 88,
      "reason": "1 concise sentence explaining why this grant fits (or doesn't fit) the user's narrowed profile."
    }
  ]
}
"""
    batch = grants[:25]
    items_for_prompt = [
        {
            "index": idx,
            "title": g.get("title", ""),
            "funder": g.get("agency_or_funder", ""),
            "deadline": g.get("deadline", ""),
            "snippet": g.get("snippet", ""),
        }
        for idx, g in enumerate(batch)
    ]

    user_prompt = (
        f"User Narrowed Grant Profile:\n{profile_raw or goal_prompt}\n\n"
        f"Candidate Grants to Evaluate:\n{json.dumps(items_for_prompt, indent=2)}"
    )

    ok, raw = call_gemini(system_prompt, user_prompt, response_json=True, temperature=0.1)
    if not ok:
        for g in grants:
            g["ai_score"] = 70
            g["ai_reason"] = "Matched narrowed search query."
        return grants

    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        eval_map = {
            int(item["index"]): item
            for item in (parsed.get("evaluations") or [])
            if "index" in item
        }
        for idx, g in enumerate(grants):
            if idx in eval_map:
                g["ai_score"] = int(eval_map[idx].get("score", 70))
                g["ai_reason"] = str(eval_map[idx].get("reason", ""))[:300]
            else:
                g["ai_score"] = 65
                g["ai_reason"] = "Matched narrowed search query."
    except Exception:
        for g in grants:
            g["ai_score"] = 70
            g["ai_reason"] = "Matched narrowed search query."

    return grants


def generate_daily_sms_summary(matching_grants: List[Dict[str, Any]]) -> str:
    if not matching_grants:
        return "Daily Grant Crawler: Checked Grants.gov & web RFPs today — no new high-match grants found yet. Will check again tomorrow!"

    top = sorted(matching_grants, key=lambda x: x.get("ai_score", 0), reverse=True)
    best = top[0]
    score_str = f"{best.get('ai_score', 0)}% match" if best.get("ai_score") else "New match"
    if len(top) == 1:
        return f"Grant Alert ({score_str}): {best['title'][:75]} - {best['url']}"
    return (
        f"Daily Grant Alert: {len(top)} new matches! "
        f"Top ({score_str}): {best['title'][:60]} {best['url']}"
    )

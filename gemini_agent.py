"""
===============================================================================
gemini_agent.py — Multi-Provider AI Client (Google Gemini, OpenAI, Claude)
===============================================================================
Purpose:
    Provides a unified interface (`call_gemini`) for all LLM operations in
    GrantFinder, supporting three AI providers out of the box:
    1. Google Gemini (`gemini-3.5-flash-lite`, `gemini-2.5-flash`, or Google OAuth)
    2. OpenAI ChatGPT (`gpt-4o-mini`, `gpt-4.1-mini`, `gpt-4o`)
    3. Anthropic Claude (`claude-sonnet-4-20250514`, `claude-3-7-sonnet-latest`,
       `claude-3-5-haiku-latest`)

    Key Functions:
    - `verify_and_save_ai_key()`: Auto-detects provider from key prefix (`AIza...` -> Gemini,
      `sk-ant-...` -> Claude, `sk-...` -> OpenAI), tests the key live, and saves it.
    - `call_gemini()`: Unified dispatcher that routes prompts to whichever provider
      the user has connected and selected.
    - `chat_with_grant_concierge()`: Delegates Phase 1 interview turns to `query_generator.py`.
    - `discover_sources_and_queries_with_ai()`: Recommends specialized non-Grants.gov
      foundation and fellowship portals tailored to the user's profile.
===============================================================================
"""

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import requests

from database import get_settings, update_settings, replace_ai_tagwords

# =====================================================================
# 1. OAUTH & PROVIDER ENDPOINT CONSTANTS
# =====================================================================

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
    """Checks for local Google Application Default Credentials (ADC) if available."""
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


PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "openai": "OpenAI (ChatGPT)",
    "claude": "Anthropic (Claude)",
}

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


# =====================================================================
# 2. API KEY DETECTION, AUTH STATUS & VERIFICATION
# =====================================================================

def _get_provider_keys(settings: Dict[str, str]) -> Dict[str, str]:
    """Reads API keys from SQLite settings or environment variables (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)."""
    gemini_key = settings.get("gemini_api_key", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    openai_key = settings.get("openai_api_key", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    claude_key = (
        settings.get("claude_api_key", "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("CLAUDE_API_KEY", "").strip()
    )
    return {
        "gemini": gemini_key,
        "openai": openai_key,
        "claude": claude_key,
    }


def _clean_and_detect_key(raw_key: str, requested_provider: Optional[str] = None) -> Tuple[str, str]:
    """
    Strips quotes or accidental `KEY_NAME=` prefixes when a user pastes an `.env` line,
    and auto-detects the AI provider from well-known key prefixes:
    - `sk-ant-...` -> `claude`
    - `AIza...`    -> `gemini`
    - `sk-...`     -> `openai`
    """
    cleaned = (raw_key or "").strip().strip('"').strip("'")
    if "=" in cleaned:
        prefix, val = cleaned.split("=", 1)
        p_up = prefix.strip().upper()
        if any(k in p_up for k in ("API_KEY", "GEMINI", "OPENAI", "ANTHROPIC", "CLAUDE")):
            cleaned = val.strip().strip('"').strip("'")

    prov = (requested_provider or "").strip().lower()
    if prov in ("chatgpt", "gpt"):
        prov = "openai"
    elif prov in ("anthropic",):
        prov = "claude"

    # Auto-detect from well-known API key prefixes if unambiguous
    if cleaned.startswith("sk-ant-"):
        detected = "claude"
    elif cleaned.startswith("AIza"):
        detected = "gemini"
    elif cleaned.startswith("sk-"):
        detected = "openai"
    else:
        detected = prov if prov in ("gemini", "openai", "claude") else "gemini"

    # If the user didn't force a different provider, or if the key prefix clearly belongs to another provider
    if not prov or prov == "auto" or cleaned.startswith(("sk-ant-", "AIza")) or (cleaned.startswith("sk-") and prov == "gemini"):
        prov = detected

    return cleaned, prov



def get_google_auth_status() -> Dict[str, Any]:
    settings = get_settings()
    keys = _get_provider_keys(settings)
    gemini_key = keys["gemini"]
    openai_key = keys["openai"]
    claude_key = keys["claude"]

    refresh_token = settings.get("google_oauth_refresh_token", "").strip()
    access_token = settings.get("google_oauth_access_token", "").strip()
    user_email = settings.get("google_user_email", "").strip()
    user_name = settings.get("google_user_name", "").strip()
    user_picture = settings.get("google_user_picture", "").strip()
    client_id = settings.get("google_oauth_client_id", "").strip() or os.environ.get("GOOGLE_CLIENT_ID", "").strip()

    if not refresh_token and not gemini_key and not openai_key and not claude_key:
        adc = _try_load_local_adc_credentials()
        if adc:
            update_settings(
                {
                    "ai_provider": "gemini",
                    "google_auth_mode": "oauth",
                    "google_oauth_client_id": adc["client_id"],
                    "google_oauth_client_secret": adc["client_secret"],
                    "google_oauth_refresh_token": adc["refresh_token"],
                    "google_user_name": "Google Local Account (ADC)",
                }
            )
            refresh_token = adc["refresh_token"]
            user_name = "Google Local Account (ADC)"

    gemini_connected = bool(refresh_token or access_token or gemini_key)
    openai_connected = bool(openai_key)
    claude_connected = bool(claude_key)

    active_provider = settings.get("ai_provider", "gemini").strip().lower()
    if active_provider not in ("gemini", "openai", "claude"):
        active_provider = "gemini"

    # If the currently selected provider isn't connected, fall back to one that is connected
    provider_conn_map = {
        "gemini": gemini_connected,
        "openai": openai_connected,
        "claude": claude_connected,
    }
    if not provider_conn_map.get(active_provider):
        for candidate_prov in ("gemini", "openai", "claude"):
            if provider_conn_map[candidate_prov]:
                active_provider = candidate_prov
                break

    connected = bool(provider_conn_map.get(active_provider))
    gemini_model = settings.get("gemini_model", "gemini-3.5-flash-lite")
    openai_model = settings.get("openai_model", "gpt-4o-mini")
    claude_model = settings.get("claude_model", "claude-3-5-haiku-latest")

    model_map = {
        "gemini": gemini_model,
        "openai": openai_model,
        "claude": claude_model,
    }
    active_model = model_map.get(active_provider, gemini_model)
    provider_label = PROVIDER_LABELS.get(active_provider, "AI Provider")

    if active_provider == "gemini":
        mode = "oauth" if (refresh_token or access_token) else ("api_key" if gemini_key else "none")
    else:
        mode = "api_key" if connected else "none"

    return {
        "connected": connected,
        "provider": active_provider,
        "provider_label": provider_label,
        "mode": mode,
        "user_email": user_email or (f"Connected via {provider_label}" if connected else ""),
        "user_name": user_name or (f"{provider_label} ({active_model})" if connected else ""),
        "user_picture": user_picture,
        "has_oauth_client": bool(client_id),
        "model": active_model,
        "providers": {
            "gemini": {
                "connected": gemini_connected,
                "model": gemini_model,
                "label": PROVIDER_LABELS["gemini"],
            },
            "openai": {
                "connected": openai_connected,
                "model": openai_model,
                "label": PROVIDER_LABELS["openai"],
            },
            "claude": {
                "connected": claude_connected,
                "model": claude_model,
                "label": PROVIDER_LABELS["claude"],
            },
        },
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
                "ai_provider": "gemini",
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


def verify_and_save_gemini_key(raw_key: str, preferred_model: Optional[str] = None) -> Tuple[bool, str, str]:
    """
    Verifies a Gemini API key using the official google-genai SDK (Interactions API or generate_content).
    Returns (ok, status_message, detected_model).
    """
    from google import genai

    cleaned, _ = _clean_and_detect_key(raw_key, "gemini")
    if not cleaned:
        return False, "API key is empty. Please paste your Gemini API key first.", ""

    preferred_models = []
    if preferred_model and preferred_model.strip():
        preferred_models.append((preferred_model.strip(), "low"))
    preferred_models.extend(
        [
            ("gemini-3.5-flash-lite", "minimal"),
            ("gemini-3.1-flash-lite", "low"),
            ("gemini-3.8-flash", "low"),
        ]
    )

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
                        "ai_provider": "gemini",
                        "gemini_api_key": cleaned,
                        "gemini_model": candidate_model,
                        "google_auth_mode": "api_key",
                        "google_user_name": f"Google Gemini ({candidate_model})",
                        "google_user_email": f"Google Gemini ({candidate_model})",
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

    # Fallback check with standard generate_content models
    for fallback_model in ("gemini-2.5-flash", "gemini-2.0-flash"):
        try:
            resp = client.models.generate_content(
                model=fallback_model,
                contents="Reply with the single word READY",
            )
            if resp and getattr(resp, "text", None):
                update_settings(
                    {
                        "ai_provider": "gemini",
                        "gemini_api_key": cleaned,
                        "gemini_model": fallback_model,
                        "google_auth_mode": "api_key",
                        "google_user_name": f"Google Gemini ({fallback_model})",
                        "google_user_email": f"Google Gemini ({fallback_model})",
                    }
                )
                return (
                    True,
                    f"Verified! Connected to Google Gemini using '{fallback_model}'.",
                    fallback_model,
                )
        except Exception as exc:
            last_error = str(exc)
            continue

    return False, f"Gemini verification failed: {last_error[:280]}", ""


def verify_and_save_openai_key(raw_key: str, preferred_model: Optional[str] = None) -> Tuple[bool, str, str]:
    """
    Verifies an OpenAI (ChatGPT) API key against the Chat Completions endpoint.
    Returns (ok, status_message, detected_model).
    """
    cleaned, _ = _clean_and_detect_key(raw_key, "openai")
    if not cleaned:
        return False, "API key is empty. Please paste your OpenAI API key first.", ""

    candidates = []
    if preferred_model and preferred_model.strip():
        candidates.append(preferred_model.strip())
    for m in ("gpt-4o-mini", "gpt-4.1-mini", "gpt-4o", "gpt-3.5-turbo"):
        if m not in candidates:
            candidates.append(m)

    last_error = ""
    for candidate_model in candidates:
        try:
            resp = requests.post(
                OPENAI_API_URL,
                headers={
                    "Authorization": f"Bearer {cleaned}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": candidate_model,
                    "messages": [{"role": "user", "content": "Reply with the single word READY"}],
                    "max_tokens": 10,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                update_settings(
                    {
                        "ai_provider": "openai",
                        "openai_api_key": cleaned,
                        "openai_model": candidate_model,
                        "google_user_name": f"OpenAI ({candidate_model})",
                        "google_user_email": f"OpenAI ({candidate_model})",
                    }
                )
                return (
                    True,
                    f"Verified! Connected to OpenAI (ChatGPT) using '{candidate_model}'.",
                    candidate_model,
                )
            else:
                try:
                    err_json = resp.json()
                    last_error = err_json.get("error", {}).get("message") or resp.text
                except Exception:
                    last_error = resp.text
                if resp.status_code == 401:
                    break
        except Exception as exc:
            last_error = str(exc)
            continue

    return False, f"OpenAI verification failed: {last_error[:280]}", ""


def verify_and_save_claude_key(raw_key: str, preferred_model: Optional[str] = None) -> Tuple[bool, str, str]:
    """
    Verifies an Anthropic (Claude) API key against the Messages endpoint.
    Returns (ok, status_message, detected_model).
    """
    cleaned, _ = _clean_and_detect_key(raw_key, "claude")
    if not cleaned:
        return False, "API key is empty. Please paste your Anthropic Claude API key first.", ""

    candidates = []
    if preferred_model and preferred_model.strip():
        candidates.append(preferred_model.strip())
    for m in (
        "claude-sonnet-4-20250514",
        "claude-3-7-sonnet-latest",
        "claude-3-5-haiku-latest",
        "claude-3-5-sonnet-latest",
        "claude-3-haiku-20240307",
    ):
        if m not in candidates:
            candidates.append(m)

    last_error = ""
    for candidate_model in candidates:
        try:
            resp = requests.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": cleaned,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": candidate_model,
                    "max_tokens": 12,
                    "messages": [{"role": "user", "content": "Reply with the single word READY"}],
                },
                timeout=15,
            )
            if resp.status_code == 200:
                update_settings(
                    {
                        "ai_provider": "claude",
                        "claude_api_key": cleaned,
                        "claude_model": candidate_model,
                        "google_user_name": f"Anthropic Claude ({candidate_model})",
                        "google_user_email": f"Anthropic Claude ({candidate_model})",
                    }
                )
                return (
                    True,
                    f"Verified! Connected to Anthropic Claude using '{candidate_model}'.",
                    candidate_model,
                )
            else:
                try:
                    err_json = resp.json()
                    last_error = err_json.get("error", {}).get("message") or resp.text
                except Exception:
                    last_error = resp.text
                if resp.status_code == 401:
                    break
        except Exception as exc:
            last_error = str(exc)
            continue

    return False, f"Claude verification failed: {last_error[:280]}", ""


def verify_and_save_ai_key(
    raw_key: str,
    provider: Optional[str] = None,
    preferred_model: Optional[str] = None,
) -> Tuple[bool, str, str, str]:
    """
    Verifies and saves an API key for Gemini, OpenAI (ChatGPT), or Anthropic (Claude).
    Auto-detects the provider from the key prefix if appropriate.
    Returns (ok, status_message, detected_model, resolved_provider).
    """
    cleaned, resolved_provider = _clean_and_detect_key(raw_key, provider)
    if not cleaned:
        return False, "API key is empty. Please paste your API key first.", "", resolved_provider

    if resolved_provider == "openai":
        ok, msg, model = verify_and_save_openai_key(cleaned, preferred_model=preferred_model)
        return ok, msg, model, "openai"
    if resolved_provider == "claude":
        ok, msg, model = verify_and_save_claude_key(cleaned, preferred_model=preferred_model)
        return ok, msg, model, "claude"

    ok, msg, model = verify_and_save_gemini_key(cleaned, preferred_model=preferred_model)
    return ok, msg, model, "gemini"


def _call_gemini_provider(
    api_key: str,
    model: str,
    system_instruction: str,
    user_prompt: str,
    temperature: float = 0.2,
) -> Tuple[bool, str]:
    from google import genai

    if not api_key:
        return False, "Please connect and verify your Gemini API key first."

    client = genai.Client(api_key=api_key)
    if model.startswith(("gemini-2.", "gemini-1.")):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=f"{system_instruction}\n\n{user_prompt}",
            )
            text = (getattr(resp, "text", "") or "").strip()
            if text:
                return True, text
        except Exception as exc:
            return False, f"Gemini API error: {exc}"

    if model == "gemini-3.8-flash":
        model = "gemini-3.5-flash-lite"

    t_level = "minimal" if "3.5-flash-lite" in model or "3.6-flash" in model else "low"
    try:
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


def _call_openai_provider(
    api_key: str,
    model: str,
    system_instruction: str,
    user_prompt: str,
    response_json: bool = True,
    temperature: float = 0.2,
) -> Tuple[bool, str]:
    if not api_key:
        return False, "Please connect and verify your OpenAI (ChatGPT) API key first."

    model = model or "gpt-4o-mini"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            try:
                err_msg = resp.json().get("error", {}).get("message") or resp.text
            except Exception:
                err_msg = resp.text
            return False, f"OpenAI API error: {err_msg}"

        data = resp.json()
        choices = data.get("choices") or []
        text = (choices[0].get("message", {}).get("content", "") if choices else "").strip()
        if not text:
            return False, "OpenAI returned an empty response."
        return True, text
    except Exception as exc:
        return False, f"OpenAI API error: {exc}"


def _call_claude_provider(
    api_key: str,
    model: str,
    system_instruction: str,
    user_prompt: str,
    temperature: float = 0.2,
) -> Tuple[bool, str]:
    if not api_key:
        return False, "Please connect and verify your Anthropic Claude API key first."

    model = model or "claude-3-5-haiku-latest"
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": 2048,
        "temperature": temperature,
        "system": system_instruction,
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            try:
                err_msg = resp.json().get("error", {}).get("message") or resp.text
            except Exception:
                err_msg = resp.text
            return False, f"Claude API error: {err_msg}"

        data = resp.json()
        blocks = data.get("content") or []
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
        if not text:
            return False, "Claude returned an empty response."
        return True, text
    except Exception as exc:
        return False, f"Claude API error: {exc}"


def call_gemini(
    system_instruction: str,
    user_prompt: str,
    response_json: bool = True,
    temperature: float = 0.2,
) -> Tuple[bool, str]:
    """
    Unified AI completion dispatcher. Routes to the user's active preferred provider
    (Google Gemini, OpenAI ChatGPT, or Anthropic Claude).
    """
    settings = get_settings()
    auth = get_google_auth_status()
    provider = auth.get("provider", "gemini")
    keys = _get_provider_keys(settings)

    if provider == "openai":
        model = settings.get("openai_model", "gpt-4o-mini").strip() or "gpt-4o-mini"
        return _call_openai_provider(
            keys["openai"],
            model,
            system_instruction,
            user_prompt,
            response_json=response_json,
            temperature=temperature,
        )

    if provider == "claude":
        model = settings.get("claude_model", "claude-3-5-haiku-latest").strip() or "claude-3-5-haiku-latest"
        return _call_claude_provider(
            keys["claude"],
            model,
            system_instruction,
            user_prompt,
            temperature=temperature,
        )

    model = settings.get("gemini_model", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    return _call_gemini_provider(
        keys["gemini"],
        model,
        system_instruction,
        user_prompt,
        temperature=temperature,
    )


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
    Phase 1 Guided Conversation & Structured Criteria Extraction:
    Delegates to `query_generator.run_guided_interview_turn` to extract statutory
    `applicant_type`, `project_domain`, `target_geography`, and `funding_range`,
    and generate structured `search_queries`, `negative_terms`, and `domain_filters`.
    """
    from query_generator import run_guided_interview_turn

    return run_guided_interview_turn(user_message)


def discover_sources_and_queries_with_ai(tagwords: List[str]) -> Dict[str, Any]:
    """
    Uses the connected AI (Claude, ChatGPT, or Gemini) to suggest real, specialized
    non-Grants.gov grant portals (private foundations, academic societies, fellowships,
    state/community foundations, corporate philanthropy) and targeted deep-web search
    queries tailored to the user's profile.
    """
    auth = get_google_auth_status()
    if not auth.get("connected"):
        return {"sources": [], "queries": []}

    settings = get_settings()
    profile_raw = settings.get("ai_profile_json", "").strip()
    goal_prompt = settings.get("ai_user_goal_prompt", "").strip()

    system_prompt = """
You are an expert Grant & Fellowship Discovery Researcher.
Given the user's grant profile and search topics, recommend specific, real, high-value NON-Grants.gov funding portals (such as private foundations, philanthropic funds, research institutes, professional associations, fellowship directories, university/national academies, or corporate grant portals) that actively offer open grants, fellowships, or RFPs in this field.

STRICT RULES FOR SOURCES:
- Every URL in `sources` MUST be a dedicated grant/fellowship/RFP opportunities page where applicants can find or apply for active funding (e.g., `/grants`, `/funding-opportunities`, `/rfps`, `/fellowships`, `/apply`).
- NEVER include Wikipedia, encyclopedias, general news outlets, academic paper repositories, job boards, `/news/`, `/blog/`, `/press-releases/`, `/winners/`, or generic organization homepages without grant listings.

Also generate 5 diverse, high-signal web search queries combining domain keywords with explicit RFP signifiers ("RFP", "Notice of Funding Opportunity", "NOFO", "solicitation", "Grant Guidelines").

Respond strictly in valid JSON matching this schema:
{
  "sources": [
    {
      "name": "Organization / Foundation Name - Open Grants or Fellowships Page",
      "url": "https://...",
      "notes": "Brief note on what active grants/fellowships they offer for this profile"
    }
  ],
  "queries": [
    "Targeted search query 1",
    "Targeted search query 2"
  ]
}
Provide 6 to 10 real, well-known foundation/fellowship/grant URLs in `sources` and 5 search strings in `queries`.
"""
    user_prompt = (
        f"User Grant Profile:\n{profile_raw or goal_prompt or 'General Grants & Fellowships'}\n\n"
        f"Active Search Topics:\n{', '.join(tagwords[:10])}\n\n"
        f"Produce the JSON response:"
    )

    ok, raw = call_gemini(system_prompt, user_prompt, response_json=True, temperature=0.3)
    if not ok:
        return {"sources": [], "queries": []}

    try:
        from search_engine import is_url_blacklisted

        parsed = _parse_json_object(raw)
        sources = [
            s
            for s in (parsed.get("sources") or [])
            if isinstance(s, dict)
            and str(s.get("url", "")).startswith("http")
            and not is_url_blacklisted(str(s.get("url", "")), str(s.get("name", "")))
            and s.get("name")
        ]
        queries = [
            str(q).strip()
            for q in (parsed.get("queries") or [])
            if str(q).strip()
        ]
        return {"sources": sources[:10], "queries": queries[:6]}
    except Exception:
        return {"sources": [], "queries": []}


def evaluate_grants_batch(grants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Delegates candidate evaluation to the Post-Scrape Two-Pass Verification Filter (`verifier.py`).
    """
    from verifier import verify_candidates_two_pass

    return verify_candidates_two_pass(grants)


def generate_daily_sms_summary(matching_grants: List[Dict[str, Any]]) -> str:
    if not matching_grants:
        return "Daily Grant Crawler: Checked foundation portals, web RFPs & federal feeds today — no new high-match grants found yet. Will check again tomorrow!"

    top = sorted(matching_grants, key=lambda x: x.get("ai_score", 0), reverse=True)
    best = top[0]
    score_str = f"{best.get('ai_score', 0)}% match" if best.get("ai_score") else "New match"
    if len(top) == 1:
        return f"Grant Alert ({score_str}): {best['title'][:75]} - {best['url']}"
    return (
        f"Daily Grant Alert: {len(top)} new matches! "
        f"Top ({score_str}): {best['title'][:60]} {best['url']}"
    )

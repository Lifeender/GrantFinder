import re
import smtplib
from email.message import EmailMessage
from typing import List, Dict, Any, Tuple
from database import CARRIER_GATEWAYS, get_settings
from gemini_agent import generate_daily_sms_summary


def _clean_phone_digits(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def get_sms_gateway_address(settings: Dict[str, str]) -> str:
    phone = _clean_phone_digits(settings.get("notify_sms_phone", ""))
    if not phone:
        return ""
    carrier = settings.get("notify_sms_carrier", "verizon").lower()
    custom_gw = settings.get("notify_sms_custom_gateway", "").strip().lstrip("@")
    domain = custom_gw if carrier == "custom" and custom_gw else CARRIER_GATEWAYS.get(carrier, "vtext.com")
    return f"{phone}@{domain}"


def _open_smtp(settings: Dict[str, str]) -> smtplib.SMTP:
    host = settings.get("smtp_host", "smtp.gmail.com").strip()
    port = int(settings.get("smtp_port", "587") or 587)
    user = settings.get("smtp_user", "").strip()
    password = settings.get("smtp_password", "").strip()

    if not host or not user or not password:
        raise ValueError("SMTP host, username, and password (or App Password) are required.")

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()

    server.login(user, password)
    return server


def send_crawl_notifications(
    new_grants: List[Dict[str, Any]],
    new_sources: List[Dict[str, Any]],
    force_daily_summary: bool = False,
) -> Tuple[bool, str]:
    """
    Sends email and/or daily SMS notifications when new AI-matched grants are discovered.
    """
    settings = get_settings()
    send_empty = settings.get("send_daily_empty_sms", "false").lower() == "true"

    if not new_grants and not new_sources and not (force_daily_summary and send_empty):
        return True, "No new high-match grants to notify."

    email_enabled = settings.get("notify_email_enabled", "true").lower() == "true"
    sms_enabled = settings.get("notify_sms_enabled", "false").lower() == "true"

    if not email_enabled and not sms_enabled:
        return False, "Notifications are disabled in settings."

    smtp_user = settings.get("smtp_user", "").strip()
    if not smtp_user or not settings.get("smtp_password", "").strip():
        return False, "SMTP credentials not configured yet."

    # Sort grants by AI match score descending
    sorted_grants = sorted(new_grants, key=lambda g: g.get("ai_score", 0), reverse=True)

    results = []
    try:
        server = _open_smtp(settings)
    except Exception as exc:
        return False, f"SMTP connection error: {exc}"

    try:
        # 1. Send Rich Email Digest
        email_to = settings.get("notify_email_to", "").strip() or smtp_user
        if email_enabled and email_to and (sorted_grants or new_sources):
            msg = EmailMessage()
            msg["Subject"] = (
                f"[AI Grant Concierge] {len(sorted_grants)} High-Match Open Grant(s) Found Today"
            )
            msg["From"] = smtp_user
            msg["To"] = email_to

            lines = [
                "AI Grant Concierge - Daily Report",
                "=================================",
                f"High-Match Open Grants Found: {len(sorted_grants)}",
                f"New Grant Sources Discovered: {len(new_sources)}",
                "",
            ]
            if sorted_grants:
                lines.append("--- TOP MATCHING OPEN GRANTS / RFPs ---")
                for idx, g in enumerate(sorted_grants[:25], 1):
                    score_txt = f"[{g.get('ai_score', 0)}% AI Match] " if g.get("ai_score") else ""
                    lines.append(f"{idx}. {score_txt}{g['title']}")
                    if g.get("ai_reason"):
                        lines.append(f"   Why it fits: {g['ai_reason']}")
                    if g.get("agency_or_funder"):
                        lines.append(f"   Funder/Agency: {g['agency_or_funder']}")
                    if g.get("deadline"):
                        lines.append(f"   Deadline: {g['deadline']}")
                    lines.append(f"   URL: {g['url']}")
                    lines.append("")

            if new_sources:
                lines.append("--- NEW GRANT SOURCES DISCOVERED ---")
                for idx, s in enumerate(new_sources[:15], 1):
                    lines.append(f"{idx}. {s['name']} ({s.get('matched_tags', '')})")
                    lines.append(f"   URL: {s['url']}")
                    lines.append("")

            msg.set_content("\n".join(lines))

            grant_cards_html = ""
            for g in sorted_grants[:25]:
                score = g.get("ai_score", 0)
                score_badge = (
                    f"<span style='background:#dcfce7;color:#166534;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:700;margin-right:6px;'>{score}% AI Match</span>"
                    if score
                    else ""
                )
                deadline_badge = (
                    f"<span style='background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:4px;font-size:12px;'>Deadline: {g['deadline']}</span>"
                    if g.get("deadline")
                    else ""
                )
                ai_reason_html = (
                    f"<div style='font-size:13px;color:#0f766e;background:#f0fdfa;padding:6px 10px;border-radius:6px;margin-bottom:8px;'><strong>Gemini Match Analysis:</strong> {g['ai_reason']}</div>"
                    if g.get("ai_reason")
                    else ""
                )
                grant_cards_html += f"""
                <div style="border:1px solid #e2e8f0;border-radius:8px;padding:12px;margin-bottom:10px;background:#ffffff;">
                    <div style="font-weight:600;font-size:15px;margin-bottom:4px;">
                        <a href="{g['url']}" style="color:#2563eb;text-decoration:none;">{g['title']}</a>
                    </div>
                    <div style="font-size:12px;color:#64748b;margin-bottom:6px;">
                        Source: {g.get('source_name','')} {(' | Funder: ' + g['agency_or_funder']) if g.get('agency_or_funder') else ''}
                    </div>
                    {ai_reason_html}
                    {f'<div style="font-size:13px;color:#334155;margin-bottom:8px;">{g["snippet"]}</div>' if g.get("snippet") else ''}
                    <div>
                        {score_badge}
                        <span style="background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:4px;font-size:12px;margin-right:6px;">Topics: {g.get('matched_tags','')}</span>
                        {deadline_badge}
                    </div>
                </div>
                """

            html_body = f"""
            <html>
            <body style="font-family:Segoe UI,Arial,sans-serif;background:#f8fafc;padding:20px;color:#0f172a;">
                <div style="max-width:680px;margin:0 auto;">
                    <h2 style="color:#1e293b;margin-bottom:4px;">AI Grant Concierge Daily Digest</h2>
                    <p style="color:#475569;margin-top:0;">
                        Gemini evaluated today's open RFPs and found <strong>{len(sorted_grants)}</strong> high-match grant(s).
                    </p>
                    {f'<h3>Matching Open Grants &amp; RFPs</h3>{grant_cards_html}' if sorted_grants else ''}
                </div>
            </body>
            </html>
            """
            msg.add_alternative(html_body, subtype="html")
            server.send_message(msg)
            results.append(f"Email sent to {email_to}")

        # 2. Send Free SMS via Carrier Email-to-SMS Gateway
        sms_addr = get_sms_gateway_address(settings)
        if sms_enabled and sms_addr and (sorted_grants or (force_daily_summary and send_empty)):
            sms_msg = EmailMessage()
            sms_msg["From"] = smtp_user
            sms_msg["To"] = sms_addr
            sms_text = generate_daily_sms_summary(sorted_grants)
            sms_msg.set_content(sms_text[:300])
            server.send_message(sms_msg)
            results.append(f"SMS sent to {sms_addr}")

    except Exception as exc:
        return False, f"Notification send failed: {exc}"
    finally:
        try:
            server.quit()
        except Exception:
            pass

    return True, "; ".join(results) if results else "No active recipient configured."


def send_test_notification() -> Tuple[bool, str]:
    sample_grant = [
        {
            "title": "Sample Open RFP: Community Innovation & Clean Tech Grant",
            "url": "https://www.grants.gov",
            "source_name": "AI Grant Concierge Test",
            "agency_or_funder": "National Science & Community Foundation",
            "deadline": "2026-12-15",
            "snippet": "This is a test alert verifying your Set & Forget AI Grant Concierge daily text and email alerts.",
            "matched_tags": "clean energy, community development",
            "ai_score": 94,
            "ai_reason": "Direct match for your organization profile and target funding criteria.",
        }
    ]
    return send_crawl_notifications(sample_grant, [])

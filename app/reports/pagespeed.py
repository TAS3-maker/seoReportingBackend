import os
from html import escape
from urllib.parse import urlsplit

import requests
from reportlab.platypus import Paragraph


def mobile_pagespeed_audit(property_url, *, api_key=None):
    """Return a point-in-time mobile audit and a safe explanation if unavailable."""
    key = api_key or next((value.strip() for name in (
        "GOOGLE_CLOUD_CONSOLE_KEY", "PAGESPEED_API_KEY", "PAGESPEED_INSIGHTS_API_KEY",
        "PAGE_SPEED_API_KEY", "GOOGLE_API_KEY",
    ) if (value := os.getenv(name, "")).strip()), None)
    url = str(property_url or "").strip()
    if url.startswith("sc-domain:"):
        url = "https://" + url.removeprefix("sc-domain:").rstrip("/") + "/"
    elif url and "://" not in url:
        url = "https://" + url
    try:
        target = urlsplit(url)
        valid = target.scheme in ("https", "http") and target.hostname and not any(c.isspace() for c in url)
    except ValueError:
        valid = False
    if not valid:
        return None, "Mobile audit unavailable: the client needs a valid Search Console website URL."
    if not key:
        return None, "Mobile audit unavailable: the report server has no PageSpeed API key configured."

    try:
        response = requests.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params={"url": url, "strategy": "mobile", "category": "performance", "key": key},
            timeout=(5, 60),
        )
        response.raise_for_status()
        result = response.json()["lighthouseResult"]
        if result.get("runtimeError"):
            raise ValueError("Incomplete Lighthouse audit")
        score = result["categories"]["performance"]["score"]
        if not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise ValueError("Missing performance score")
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None, "The mobile audit was unavailable when this report was generated."

    metrics = [("Performance score", f"{round(score * 100)}/100")]
    for audit, label in (
        ("largest-contentful-paint", "Largest contentful paint"),
        ("total-blocking-time", "Total blocking time"),
        ("cumulative-layout-shift", "Cumulative layout shift"),
    ):
        value = result.get("audits", {}).get(audit, {}).get("displayValue")
        if value:
            metrics.append((label, str(value)))
    return (str(result.get("finalUrl") or url), str(result.get("fetchTime") or ""), metrics), None


def pagespeed_section(property_url, heading_style, body_style, *, api_key=None):
    """Build a point-in-time mobile Lighthouse summary for a report."""
    section = [Paragraph("PageSpeed Insights", heading_style)]
    audit, error = mobile_pagespeed_audit(property_url, api_key=api_key)
    if audit is None:
        section.append(Paragraph(error, body_style))
        return section

    tested_url, timestamp, metrics = audit
    section.append(Paragraph(f"Mobile Lighthouse audit &bull; {escape(tested_url)} {escape(timestamp)}", body_style))
    metrics = [("Performance", metrics[0][1]), *metrics[1:]]
    section.append(Paragraph(" &bull; ".join(f"<b>{label}</b> {escape(value)}" for label, value in metrics), body_style))
    return section

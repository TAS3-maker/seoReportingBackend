import json
import logging
import math
import os

import requests


from app.reports.formatting import report_number, report_position

logger = logging.getLogger(__name__)

# Each supported KPI has a defined scope and direction of improvement.
_METRICS = {
    "gsc_clicks": ("Visibility", "Organic search clicks",
                   "Search Console counts clicks from unpaid Google Search results to the site."),
    "gsc_impressions": ("Visibility", "Search impressions",
                        "Impressions show how often a result from the site appeared in Google Search."),
    "gsc_ctr": ("Visibility", "Search click-through rate",
                "This shows what share of Google Search impressions turned into clicks."),
    "gsc_position": ("Rankings", "Average search position",
                     "A lower average position means the site's top result appeared closer to the top."),
    "ga4_totalUsers": ("Audience", "Website users",
                       "This measures the audience GA4 recorded across all traffic channels."),
    "ga4_sessions": ("Traffic", "Website sessions",
                     "Sessions show how many visits GA4 recorded across all traffic channels."),
    "ga4_screenPageViews": ("Engagement", "Page views",
                            "Views count how often pages were viewed, including repeat views."),
}

_DECLINE_CONTEXT = {
    "gsc_clicks": "Fewer clicks mean Google Search results sent fewer visits to the site during the period.",
    "gsc_impressions": "Fewer impressions mean the site appeared less often in Google Search.",
    "gsc_ctr": "A smaller share of Google Search impressions turned into clicks.",
    "gsc_position": "The site's top result appeared lower in Google Search on average.",
    "ga4_totalUsers": "GA4 recorded a smaller website audience across all measured traffic channels.",
    "ga4_sessions": "GA4 recorded fewer visits across all measured traffic channels.",
    "ga4_screenPageViews": "GA4 recorded fewer page views, including views from repeat visits.",
}


def report_signals(kpis, hide_comparison, token, *, include_declines=False):
    signals = {}
    for kpi in kpis:
        key = kpi.get("key")
        current = kpi.get("value")
        if key not in _METRICS or not isinstance(current, (int, float)) or not math.isfinite(current):
            continue
        previous = kpi.get("previous_value")
        unit = "%" if key == "gsc_ctr" else ""
        formatter = report_position if key == "gsc_position" else report_number
        if key == "gsc_ctr" and include_declines:
            formatter = lambda number: f"{number:.2f}"
        value = f"{formatter(current)}{unit}"
        category, title, context = _METRICS[key]
        direction = "up"
        if hide_comparison:
            if current <= 0:
                continue
            fact = f"{value} in the reporting period."
        else:
            if not isinstance(previous, (int, float)) or not math.isfinite(previous):
                continue
            improved = current < previous if key == "gsc_position" else current > previous
            if current == previous or (not improved and not include_declines):
                continue
            if key == "gsc_position" and min(previous, current) <= 0:
                continue
            direction = "up" if improved else "down"
            if not improved:
                context = _DECLINE_CONTEXT[key]
            change = "Improved" if improved else "Worsened" if key == "gsc_position" else "Declined"
            if key == "gsc_position" and formatter(previous) == formatter(current):
                fact = f"Average position {change.lower()} by less than 0.1 to {value}."
            elif formatter(previous) == formatter(current):
                if not include_declines:
                    continue
                amount = "0.01 percentage points" if key == "gsc_ctr" else "0.1"
                fact = f"{change} by less than {amount}. Current value: {value}."
            else:
                fact = f"{change} from {formatter(previous)}{unit} to {value}."
        signals[key] = {"category": category, "title": title, "fact": fact, "context": context, "direction": direction}

    if not signals or not token:
        return list(signals.values())

    properties = {"metric": {"type": "string", "enum": list(signals)}, "meaning": {"type": "string"}}
    schema = {"type": "object", "additionalProperties": False, "required": ["signals"],
              "properties": {"signals": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "properties": properties, "required": list(properties),
              }}}}
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": os.getenv("OPENAI_REPORT_MODEL") or "gpt-4o-mini",
                "store": False,
                "instructions": (
                    ("Explain every verified improvement and decline supplied. " if include_declines else
                     "Choose up to four distinct, most informative SEO signals from these verified facts. ") +
                    "For each add one short sentence explaining why the verified result matters. "
                    "Use the metric context for scope and avoid repeating the fact. "
                    "Use no numbers, predictions, causal claims or claims about other metrics. "
                    "GA4 metrics cover all channels. Do not claim a change unless the fact states one."
                ),
                "input": json.dumps(signals),
                "text": {"format": {"type": "json_schema", "name": "report_signals",
                                    "strict": True, "schema": schema}},
            },
            timeout=(4, 20),
        )
        response.raise_for_status()
        result = response.json()
        output = next(part["text"] for item in result["output"] for part in item.get("content", [])
                      if part.get("type") == "output_text")
        selected = {}
        for item in json.loads(output)["signals"]:
            key = item["metric"]
            meaning = item["meaning"].strip()
            if key in signals and meaning and len(meaning) <= 220 and not any(c.isdigit() for c in meaning):
                selected[key] = dict(signals[key], meaning=meaning)
        if include_declines:
            return [selected.get(key, signal) for key, signal in signals.items()]
        if selected:
            return list(selected.values())[:4]
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        logger.warning("OpenAI report insights unavailable; using verified KPI facts.")
    return list(signals.values())

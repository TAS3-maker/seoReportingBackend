"""Shared weekly and monthly PDF layout for analytics dashboard data."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html import escape
from io import BytesIO
from math import ceil, isfinite
from typing import Any, Literal
from urllib.parse import urlsplit

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from app.reports.formatting import report_number, report_position
from app.reports.insights import DECLINE_NEXT_STEPS, report_signals
from app.reports.pagespeed import pagespeed_audit

_PAGE_WIDTH, _PAGE_HEIGHT = A4
_MARGIN = 36
_WIDTH = _PAGE_WIDTH - 2 * _MARGIN
_GAP = 12
_HALF = (_WIDTH - _GAP) / 2
_THIRD = (_WIDTH - 2 * _GAP) / 3
_BOTTOM = 789
_NAVY = HexColor("#10283F")
_INK = HexColor("#183249")
_MUTED = HexColor("#4B6577")
_TEAL = HexColor("#087A70")
_AQUA = HexColor("#53CABC")
_PAPER = HexColor("#F1F6F8")
_GRID = HexColor("#D7E4EA")
_STRIPE = HexColor("#EAF1F5")
_NOTE = HexColor("#E8EFF6")
_GREEN = HexColor("#DFF3EA")
_INSIGHT_GREEN = HexColor("#086345")
_SLATE_BLUE = HexColor("#38546E")
_BLUE = HexColor("#365E9D")
_FONT = "Helvetica"
_BOLD = "Helvetica-Bold"
_MISSING = "-"
_MEASURES = {
    "gsc_clicks": "Google search clicks",
    "gsc_impressions": "Search impressions",
    "gsc_ctr": "Search click-through rate",
    "gsc_position": "Average search position",
    "ga4_totalUsers": "GA4 users (all channels)",
    "ga4_sessions": "GA4 sessions (all channels)",
    "ga4_screenPageViews": "GA4 page views (all channels)",
}
_HEADERS = {
    "date": "Date", "page": "Page", "query": "Search query", "clicks": "Clicks",
    "impressions": "Impr.", "ctr_percent": "CTR", "position": "Position",
    "totalUsers": "Users", "sessions": "Sessions", "screenPageViews": "Views",
}


def _date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _range(start, end):
    if not start or not end:
        return "Reporting dates unavailable"
    if start.replace(day=1) == end.replace(day=1):
        return f"{start.day}-{end.day} {end:%B %Y}"
    return f"{start:%d %b %Y} - {end:%d %b %Y}"


def _number(value):
    return isinstance(value, (int, float)) and isfinite(value)


def _metric(key, value):
    if value is None or value == "":
        return _MISSING
    normalized = str(key).lower().replace("_", "").replace(" ", "").replace(".", "")
    if normalized in ("gscposition", "position", "avgposition", "averageposition", "averagesearchposition"):
        return report_position(value)
    if _number(value):
        if key in ("gsc_ctr", "ctr_percent"):
            return f"{value:.2f}%" if key == "gsc_ctr" else f"{report_number(value)}%"
        return report_number(value)
    if key == "date" and (day := _date(value)):
        return day.strftime("%d %b")
    return str(value)


def _change(kpi):
    value = kpi.get("change")
    if not _number(value) or kpi.get("trend") == "unavailable":
        return _MISSING, _MUTED
    if not value:
        return "No change", _MUTED
    magnitude = report_number(abs(value))
    improved = kpi.get("trend") == "up"
    if kpi.get("change_unit") == "positions":
        return f"{magnitude} positions {'better' if improved else 'worse'}", _TEAL if improved else _SLATE_BLUE
    percentage_points = kpi.get("change_unit") == "percentage_points"
    amount = f"{abs(value):.2f} pp" if percentage_points else f"{magnitude}%"
    return f"{'Up' if value > 0 else 'Down'} {amount}", _TEAL if improved else _SLATE_BLUE


def _signal_detail(signal):
    return " ".join(filter(None, (signal["fact"], signal.get("meaning") or signal.get("context"))))


def _keyword_comparison_column(label):
    normalized = "".join(character for character in str(label).lower() if character.isalnum())
    return any(part in normalized for part in (
        "change", "delta", "previous", "prior", "lastmonth", "monthovermonth", "mom",
        "lastweek", "weekoverweek", "wow",
    ))


class _PeriodReport:
    def __init__(self, output, dashboard, period, client_name, comparison):
        self.weekly = period == "weekly"
        self.period_unit = "week" if self.weekly else "month"
        self.report_title = f"{period.title()} SEO Report"
        self.canvas = Canvas(output, pagesize=A4)
        self.canvas.setTitle(f"{client_name} {self.report_title}")
        self.canvas.setAuthor("")
        self.canvas.setCreator("")
        self.canvas.setProducer("")
        self.dashboard = dashboard
        self.client_name = client_name
        self.show_comparison = not comparison
        self.kpis = {kpi.get("key"): kpi for kpi in dashboard.get("kpis", [])}
        self.tables = dashboard.get("tables", {})
        self.site_url = dashboard.get("sources", {}).get("gsc", {}).get("site_url") or ""
        self.site = self.site_url.removeprefix("sc-domain:")
        if "://" in self.site:
            try:
                self.site = urlsplit(self.site).hostname or self.site
            except ValueError:
                pass
        dates = dashboard.get("period", {})
        self.start = _date(dates.get("start_date"))
        self.end = _date(dates.get("end_date"))
        self.previous_start = _date(dates.get("comparison_start_date"))
        self.previous_end = _date(dates.get("comparison_end_date"))
        self.period_label = (
            _range(self.start, self.end) if self.weekly else
            self.start.strftime("%B %Y") if self.start else "Reporting period"
        )
        self.current = self._comparison_label(self.start, self.end, "Current period")
        self.previous = self._comparison_label(self.previous_start, self.previous_end, "Previous period")
        self.pages = []
        self.section = None

    def _comparison_label(self, start, end, fallback):
        if self.weekly:
            return _range(start, end) if start and end else fallback
        return start.strftime("%B") if start else fallback

    def paragraph(self, value, *, size=9, bold=False, color=_INK):
        return Paragraph(escape(str(value)), ParagraphStyle(
            "ReportText", fontName=_BOLD if bold else _FONT, fontSize=size,
            leading=size * 1.23, textColor=color, splitLongWords=True,
        ))

    def text(self, value, x, top, width=_WIDTH, **style):
        item = self.paragraph(value, **style)
        _, height = item.wrap(width, _PAGE_HEIGHT)
        item.drawOn(self.canvas, x, _PAGE_HEIGHT - top - height)
        return height

    def box(self, x, top, width, height, color=white, *, radius=8, border=False):
        self.canvas.setFillColor(color)
        self.canvas.setStrokeColor(_GRID)
        self.canvas.setLineWidth(0.6)
        self.canvas.roundRect(x, _PAGE_HEIGHT - top - height, width, height, radius,
                              fill=1, stroke=int(border))

    def line(self, x1, top, x2, color=_GRID):
        self.canvas.setStrokeColor(color)
        self.canvas.setLineWidth(0.65)
        self.canvas.line(x1, _PAGE_HEIGHT - top, x2, _PAGE_HEIGHT - top)

    def page(self, section=None, title=None, subtitle=None):
        if self.section is not None:
            self.pages.append(dict(self.canvas.__dict__))
            self.canvas._startPage()
        self.section = (section, title, subtitle)
        self.canvas.setFillColor(_PAPER)
        self.canvas.rect(0, 0, _PAGE_WIDTH, _PAGE_HEIGHT, fill=1, stroke=0)
        self.text("SEO REPORT", _MARGIN, 33, size=12, bold=True, color=_TEAL)
        label = f"{self.client_name} / {self.period_label}".upper()
        while stringWidth(label, _BOLD, 7.4) > _WIDTH - 105:
            label = label[:-5].rstrip() + "..."
        self.canvas.setFont(_BOLD, 7.4)
        self.canvas.setFillColor(_MUTED)
        self.canvas.drawRightString(_PAGE_WIDTH - _MARGIN, _PAGE_HEIGHT - 43, label)
        self.line(_MARGIN, 62, _PAGE_WIDTH - _MARGIN)
        if title:
            self.text(section, _MARGIN, 82, size=9, bold=True, color=_TEAL)
            height = self.text(title, _MARGIN, 102, size=28, bold=True, color=_NAVY)
            self.text(subtitle, _MARGIN, 110 + height, size=10, color=_MUTED)

    def finish(self):
        self.pages.append(dict(self.canvas.__dict__))
        for number, state in enumerate(self.pages, 1):
            self.canvas.__dict__.update(state)
            self.line(_MARGIN, 800, _PAGE_WIDTH - _MARGIN)
            self.text(f"{self.report_title.upper()} / {self.period_label.upper()}", _MARGIN, 808,
                      size=7.4, bold=True, color=_MUTED)
            self.canvas.setFillColor(_MUTED)
            self.canvas.setFont(_BOLD, 7.4)
            self.canvas.drawRightString(_PAGE_WIDTH - _MARGIN, _PAGE_HEIGHT - 816,
                                        f"{number:02d} / {len(self.pages):02d}")
            self.canvas.showPage()
        self.canvas.save()

    def note(self, title, detail, x, top, width=_WIDTH, minimum=55, color=_NOTE, *, text_color=None):
        heading = self.paragraph(title, size=9.5, bold=True, color=text_color or _INK)
        body = self.paragraph(detail, size=9, color=text_color or _MUTED)
        heights = [item.wrap(width - 24, _PAGE_HEIGHT)[1] for item in (heading, body)]
        height = max(minimum, 25 + sum(heights))
        self.box(x, top, width, height, color)
        heading.drawOn(self.canvas, x + 12, _PAGE_HEIGHT - top - 10 - heights[0])
        body.drawOn(self.canvas, x + 12, _PAGE_HEIGHT - top - 14 - sum(heights))
        return top + height

    def series(self, source, metric):
        chart = self.dashboard.get("charts", {}).get(source, {})
        values = next((series.get("data", []) for series in chart.get("datasets", [])
                       if series.get("label") == metric), [])
        return list(zip(chart.get("labels", []), values))

    def chart(self, title, series, x, top, width, height, *, color=_TEAL,
              bars=False, dots=False, reference=None, subtitle=None):
        self.box(x, top, width, height, border=True)
        self.text(title, x + 12, top + 10, width - 24, size=11, bold=True)
        if subtitle:
            self.text(subtitle, x + 12, top + 26, width - 24, size=8, color=_MUTED)
        left, right = x + 30, x + width - 18
        upper, lower = top + (50 if subtitle else 33), top + height - (38 if reference is not None else 22)
        values = [value for _, value in series if _number(value)]
        if not values:
            self.text("No daily data available", x + 12, (upper + lower) / 2, width - 24, color=_MUTED)
            return
        steps = 4 if height >= 170 else 2
        peak = max(values + ([reference] if reference is not None else []))
        tick = max(1, ceil(peak / steps))
        maximum = tick * steps
        for i in range(steps + 1):
            level = lower - (lower - upper) * i / steps
            self.line(left, level, right)
            self.canvas.setFillColor(_MUTED)
            self.canvas.setFont(_FONT, 7)
            self.canvas.drawRightString(left - 4, _PAGE_HEIGHT - level - 2, report_number(i * tick))
        if reference is not None:
            level = lower - reference / maximum * (lower - upper)
            self.canvas.saveState()
            self.canvas.setDash(3, 2)
            self.line(left, level, right, _MUTED)
            self.canvas.restoreState()
            self.text(f"Dashed: previous period average, {reference:.1f} per day",
                      x + 12, top + height - 16, width - 24, size=7.5, color=_MUTED)
        previous = None
        for i, (label, value) in enumerate(series):
            fraction = (i + 0.5) / len(series) if bars else i / max(1, len(series) - 1)
            px = left + (right - left) * fraction
            if bars or i in (0, len(series) - 1):
                day = _date(label)
                self.canvas.setFillColor(_MUTED)
                self.canvas.setFont(_FONT, 7)
                self.canvas.drawCentredString(px, _PAGE_HEIGHT - lower - 12,
                                              f"{day.day} {day:%b}" if day else str(label))
            if not _number(value):
                previous = None
                continue
            py = _PAGE_HEIGHT - lower + (lower - upper) * value / maximum
            self.canvas.setStrokeColor(color)
            self.canvas.setFillColor(color)
            self.canvas.setLineWidth(1.05)
            if bars:
                w = min(30, (right - left) / len(series) * 0.66)
                self.canvas.roundRect(px - w / 2, _PAGE_HEIGHT - lower, w,
                                      max(1, py - (_PAGE_HEIGHT - lower)), 2, stroke=0, fill=1)
                self.canvas.setFillColor(_INK)
                self.canvas.setFont(_BOLD, 8)
                self.canvas.drawCentredString(px, py + 5, report_number(value))
            elif dots or len(values) == 1:
                self.canvas.circle(px, py, 1.6, stroke=0, fill=1)
            elif previous:
                self.canvas.line(*previous, px, py)
            previous = (px, py)

    def table(self, labels, rows, width, *, weights=None, compact=False):
        header = [self.paragraph(value, size=8, bold=True, color=white) for value in labels]
        size = 7.8 if compact else 8.7
        body = [[value if isinstance(value, Paragraph) else self.paragraph(value, size=size, bold=i == 0)
                 for i, value in enumerate(row)]
                for row in rows]
        weights = weights or [1] * len(labels)
        padding = 2.7 if compact else 7.1
        table = Table([header, *body], colWidths=[width * w / sum(weights) for w in weights],
                      repeatRows=1, splitInRow=1, cornerRadii=[4, 4, 0, 0])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, _STRIPE]),
            ("TOPPADDING", (0, 0), (-1, -1), padding),
            ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return table

    def keyword_data(self):
        data = self.tables.get("google_sheets", {})
        columns = [column for column in data.get("columns", [])
                   if self.show_comparison or not _keyword_comparison_column(column)]
        rows = [row for row in data.get("rows", [])
                if any(row.get(column) is not None and str(row[column]).strip() for column in columns)]
        return columns, rows

    def data_table(self, key, width, *, compact=False):
        data = self.tables.get(key, {})
        columns = data.get("columns", [])
        rows = data.get("rows", [])
        if not columns or not rows:
            return None
        labels = [_HEADERS.get(c, c) for c in columns]
        weights = [max(2, len(columns) - 2)] + [1] * (len(columns) - 1) if key in ("top_pages", "top_queries") else None
        values = [[_metric(c, row.get(c)) for c in columns] for row in rows]
        return self.table(labels, values, width, weights=weights, compact=compact)

    def draw_table(self, table, x, top, width):
        if table is None:
            return top + self.text("No stored data for this period.", x, top, width, color=_MUTED)
        _, height = table.wrap(width, _BOTTOM - top)
        table.drawOn(self.canvas, x, _PAGE_HEIGHT - top - height)
        return top + height

    def table_section(self, title, table, top, description=None, *, link=None):
        if top > 695:
            self.page(*self.section)
            top = 176
        top += self.text(title, _MARGIN, top, size=15, bold=True) + 5
        if description:
            width = _WIDTH
            if link:
                label, url = link
                label_width = stringWidth(label, _BOLD, 9) + 2
                label_x = _MARGIN + _WIDTH - label_width
                linked_label = Paragraph(f'<link href="{escape(url, quote=True)}"><u>{escape(label)}</u></link>',
                                         ParagraphStyle("KeywordLink", fontName=_BOLD, fontSize=9,
                                                        leading=11.07, textColor=_TEAL))
                linked_label.wrap(label_width, _PAGE_HEIGHT)
                linked_label.drawOn(self.canvas, label_x, _PAGE_HEIGHT - top - linked_label.height)
                width -= label_width + _GAP
            top += self.text(description, _MARGIN, top, width, size=9, color=_MUTED)
        top += 6
        while table is not None:
            if table.wrap(_WIDTH, _BOTTOM - top)[1] <= _BOTTOM - top:
                break
            parts = table.split(_WIDTH, _BOTTOM - top)
            if len(parts) == 2:
                self.draw_table(parts[0], _MARGIN, top, _WIDTH)
                table = parts[1]
            self.page(*self.section)
            top = 176
            if not parts and not table.split(_WIDTH, _BOTTOM - top):
                raise ValueError(f"Report table cannot fit on a page: {title}")
        return self.draw_table(table, _MARGIN, top, _WIDTH) + 18

    def cover(self, improvements, declines, plans, audits):
        self.page()
        self.box(_MARGIN, 82, _WIDTH, 207, _NAVY, radius=18)
        self.box(53, 107, 4, 24, _AQUA, radius=2)
        self.text(self.period_label.upper(), 69, 112, size=9.5, bold=True, color=_AQUA)
        self.text(self.report_title, 60, 157, _WIDTH - 48, size=35, bold=True, color=white)
        client_label = " / ".join(filter(None, (self.client_name, self.site)))
        title = self.paragraph(client_label, size=12, bold=True, color=white)
        _, label_height = title.wrap(_WIDTH - 50, _PAGE_HEIGHT)
        label_top = min(235, 253 - label_height)
        title.drawOn(self.canvas, 61, _PAGE_HEIGHT - label_top - label_height)
        span = _range(self.start, self.end)
        if self.show_comparison and self.previous_start and self.previous_end:
            span += f"  /  Compared with {_range(self.previous_start, self.previous_end)}"
        self.text(span, 61, 262, _WIDTH - 50, size=9.2, color=HexColor("#CCE1EA"))

        featured = ("gsc_impressions", "gsc_clicks", "gsc_ctr")
        for i, key in enumerate(featured):
            x = _MARGIN + i * (_THIRD + _GAP)
            self.box(x, 307, _THIRD, 103, border=True)
            self.text(_MEASURES[key], x + 12, 319, _THIRD - 24, size=9, bold=True, color=_MUTED)
            value = _metric(key, self.kpis.get(key, {}).get("value"))
            value_width = stringWidth(value, _BOLD, 24)
            value_size = 24 * min(1, (_THIRD - 24) / max(value_width, 1))
            self.text(value, x + 12, 335, _THIRD - 24, size=value_size, bold=True, color=_NAVY)
            series = self.series("gsc_daily", key.removeprefix("gsc_"))
            values = [value for _, value in series if _number(value)]
            if values and value_width < _THIRD - 82:
                self.canvas.setStrokeColor(_TEAL)
                self.canvas.setLineWidth(0.9)
                self.canvas.lines([(x + _THIRD - 67 + 52 * j / max(len(values) - 1, 1),
                                    _PAGE_HEIGHT - 370 + 18 * value / max(max(values), 1),
                                    x + _THIRD - 67 + 52 * (j + 1) / max(len(values) - 1, 1),
                                    _PAGE_HEIGHT - 370 + 18 * next_value / max(max(values), 1))
                                   for j, (value, next_value) in enumerate(zip(values, values[1:]))])
            if self.show_comparison:
                label, color = _change(self.kpis.get(key, {}))
                tag_width = stringWidth(label, _BOLD, 8) + 14
                self.box(x + 12, 382, tag_width, 16, _NOTE if color == _SLATE_BLUE else _GREEN)
                self.text(label, x + 19, 385, tag_width, size=8, bold=True, color=color)

        self.text(f"Whole {self.period_unit} in 30 seconds", _MARGIN, 433, size=15, bold=True)
        top = 461
        summary = self.period_summary(plans, audits)
        if improvements or declines:
            summary += (("Period changes", f"{len(improvements)} metrics improved and {len(declines)} declined."),)
        for title, detail in summary:
            self.text(title, _MARGIN, top, 116, size=9.5, bold=True, color=_TEAL)
            snippet = detail if len(detail) <= 200 else detail[:197] + "..."
            top += max(13, self.text(snippet, _MARGIN + 120, top, _WIDTH - 120, color=_MUTED)) + 8
        featured_signals = improvements[:3]
        if not featured_signals:
            return 0
        body_height = max(self.paragraph(_signal_detail(signal)).wrap(_THIRD - 24, _PAGE_HEIGHT)[1]
                          for signal in featured_signals)
        card_height = max(105, 38 + body_height)
        if top + 25 + card_height > _BOTTOM:
            return 0
        top += self.text("Positive signals", _MARGIN, top + 4, size=15, bold=True) + 14
        for i, signal in enumerate(featured_signals):
            self.note(signal["title"], _signal_detail(signal), _MARGIN + i * (_THIRD + _GAP), top,
                      _THIRD, minimum=card_height, color=_INSIGHT_GREEN, text_color=white)
        return len(featured_signals)

    def period_summary(self, plans, audits):
        def totals(fields):
            values = [(title, _metric(key, self.kpis.get(key, {}).get("value"))) for key, title in fields]
            return ", ".join(f"{title}: {value}" for title, value in values if value != _MISSING) or "No totals available."

        search = totals((("gsc_clicks", "Clicks"), ("gsc_impressions", "Impressions"),
                         ("gsc_ctr", "CTR"), ("gsc_position", "Average position")))
        audience = totals((("ga4_totalUsers", "Users"), ("ga4_sessions", "Sessions"),
                           ("ga4_screenPageViews", "Page views")))
        channels = {name: count for name, count in (self.dashboard.get("visitor_channels") or {}).items()
                    if _number(count) and count > 0}
        if channels:
            channel = max(channels, key=channels.get)
            audience += f" / Top acquisition channel: {channel} ({report_number(channels[channel])} users)"

        pages = len(self.tables.get("top_pages", {}).get("rows", []))
        queries = len(self.tables.get("top_queries", {}).get("rows", []))
        keyword_columns, keyword_rows = self.keyword_data()
        rankings = [f"{pages} top pages and {queries} search queries shown."] if pages or queries else []
        if keyword_rows:
            rankings.append(f"{len(keyword_rows)} tracked keyword rows available.")
            first = {name.casefold().strip(): keyword_rows[0].get(name) for name in keyword_columns}
            keyword = first.get("keyword") or first.get("target keyword")
            position = first.get("position") or first.get("current position") or first.get("rank")
            if keyword and position:
                rankings.append(f"Tracked example: {keyword} at position {_metric('position', position)}.")
        speed = []
        for strategy, (audit, _) in audits.items():
            score = dict(audit[2]).get("Performance score", "Unavailable") if audit else "Unavailable"
            speed.append(f"{strategy.title()}: {score}")
        gsc_days = len(self.tables.get("gsc_daily", {}).get("rows", []))
        ga4_days = len(self.tables.get("ga4_daily", {}).get("rows", []))
        summary = [
            ("Search performance", search),
            ("Audience and traffic", audience),
        ]
        if rankings:
            summary.append(("Rankings and keywords", " ".join(rankings)))
        summary.append(("PageSpeed scores", " / ".join(speed)))
        if plans:
            activity = "activity" if len(plans) == 1 else "activities"
            summary.append((f"Next {self.period_unit}", f"{len(plans)} planned {activity}. First: {plans[0]}"))
        summary.append(("Daily source data", f"{gsc_days} Search Console rows and {ga4_days} GA4 rows in the appendix."))
        return tuple(summary)

    def performance(self):
        if self.weekly:
            title = "Weekly performance"
        else:
            title = f"{self.current} vs. {self.previous}" if self.show_comparison else self.current
        subtitle = "Search Console and GA4 metrics are shown separately."
        if self.show_comparison:
            subtitle += " Change in click-through rate is shown in percentage points (pp)."
        self.page("PERFORMANCE", title, subtitle)
        labels = ["Measure", self.current]
        rows = []
        if self.show_comparison:
            labels.extend([self.previous, "Change"])
        for key, label in _MEASURES.items():
            kpi = self.kpis.get(key, {})
            row = [label, _metric(key, kpi.get("value"))]
            if self.show_comparison:
                change, color = _change(kpi)
                row.extend([_metric(key, kpi.get("previous_value")),
                            self.paragraph(change, size=8.7, bold=True, color=color)])
            rows.append(row)
        rows.append(["Organic Search sessions"] + [_MISSING] * (len(labels) - 1))
        table = self.table(labels, rows, _WIDTH, weights=[2.2, 1, 1, 1.4][:len(labels)])
        top = self.draw_table(table, _MARGIN, 177, _WIDTH) + 12
        top += self.text("GA4 metrics cover all channels. Organic Search sessions are unavailable in the stored summary.",
                         _MARGIN, top, size=8.5, color=_MUTED) + 12
        clicks = self.series("gsc_daily", "clicks")
        weeks = {}
        if not self.weekly:
            for label, value in clicks:
                day = _date(label)
                if day and self.start and _number(value):
                    offset = (day - self.start).days
                    if offset >= 0:
                        group = offset // 7
                        weeks[group] = weeks.get(group, 0) + value
        # Keep long custom ranges readable while retaining their full daily breakdown in the appendix.
        if self.weekly or len(weeks) > 7:
            self.chart("Daily search clicks", clicks,
                       _MARGIN, top, _HALF, 209, subtitle=self.period_label)
        else:
            days = (self.end - self.start).days + 1 if self.start and self.end else 31
            weekly = [(f"{7 * i + 1}-{min(7 * i + 7, days)}", value) for i, value in sorted(weeks.items())]
            self.chart("Clicks by reporting week", weekly, _MARGIN, top, _HALF, 209,
                       bars=True, subtitle=self.period_label)
        previous = self.kpis.get("gsc_impressions", {}).get("previous_value")
        reference = None
        if self.show_comparison and _number(previous) and self.previous_start and self.previous_end:
            reference = previous / max(1, (self.previous_end - self.previous_start).days + 1)
        self.chart("Daily search impressions", self.series("gsc_daily", "impressions"),
                   _MARGIN + _HALF + _GAP, top, _HALF, 209, subtitle=self.period_label, reference=reference)
        if not self.show_comparison:
            return
        top += 221
        for i, (key, context, change_context) in enumerate((
            ("gsc_ctr", "This shows the share of Google Search impressions that turned into clicks.",
             "A {} share of Google Search impressions turned into clicks."),
            ("gsc_position", "A lower number means the site's top result appeared closer to the top of Google Search.",
             "The site's top result appeared {} on the Google Search results page on average."),
        )):
            kpi = self.kpis.get(key, {})
            current = _metric(key, kpi.get("value"))
            previous = _metric(key, kpi.get("previous_value"))
            if current == _MISSING:
                detail = "No data is available for this metric in the reporting period."
            elif previous != _MISSING:
                value, prior = kpi.get("value"), kpi.get("previous_value")
                if current != previous and _number(value) and _number(prior):
                    declined = value < prior if key == "gsc_ctr" else value > prior
                    if declined:
                        context = f"Next step: {DECLINE_NEXT_STEPS[key]}"
                    else:
                        context = change_context.format("larger" if key == "gsc_ctr" else "higher")
                detail = f"{current} this period, compared with {previous} last period. {context}"
            else:
                detail = f"{current} this period. {context}"
            self.note(_MEASURES[key], detail, _MARGIN + i * (_HALF + _GAP), top, _HALF, minimum=80)

    def audience(self):
        self.page("AUDIENCE", "Traffic and engagement",
                  "See how visitors first found the website and how activity changed during the reporting period.")
        self.text("Where visitors first came from", _MARGIN, 178, size=15, bold=True)
        self.text("GA4 first-user acquisition channel", _MARGIN, 202, size=9, color=_MUTED)
        channels = sorted(((name, value) for name, value in (self.dashboard.get("visitor_channels") or {}).items()
                           if _number(value) and value > 0), key=lambda pair: (-pair[1], pair[0]))
        if len(channels) > 6:
            channels = channels[:5] + [("Remaining channels", sum(value for _, value in channels[5:]))]
        channel_height = 186 if channels else 48
        self.box(_MARGIN, 223, _WIDTH, channel_height, border=True)
        if channels:
            total = sum(value for _, value in channels)
            start = 90
            palette = (_TEAL, _BLUE, HexColor("#A87223"), HexColor("#8462A3"),
                       HexColor("#528391"), _MUTED)
            cx, cy = 125, _PAGE_HEIGHT - 312
            for i, (name, value) in enumerate(channels):
                arc = -360 * value / total
                self.canvas.setFillColor(palette[i])
                self.canvas.wedge(cx - 61, cy - 61, cx + 61, cy + 61, start, arc, stroke=0, fill=1)
                start += arc
                y = 242 + 21 * i
                self.box(217, y + 3, 8, 8, palette[i], radius=2)
                self.text(name, 232, y, 170, size=9, bold=True)
                self.canvas.setFillColor(_MUTED)
                self.canvas.setFont(_FONT, 9)
                self.canvas.drawRightString(_PAGE_WIDTH - 52, _PAGE_HEIGHT - y - 9,
                                            f"{report_number(value)} users / {100 * value / total:.1f}%")
                self.line(232, y + 16, _PAGE_WIDTH - 52)
            self.canvas.setFillColor(white)
            self.canvas.circle(cx, cy, 44, fill=1, stroke=0)
            self.canvas.setFillColor(_NAVY)
            self.canvas.setFont(_BOLD, 26)
            self.canvas.drawCentredString(cx, cy, report_number(total))
            self.canvas.setFont(_FONT, 8)
            self.canvas.drawCentredString(cx, cy - 15, "GA4 users")
            leader, users = channels[0]
            note = f"{leader} was the largest first-user channel, with {report_number(users)} of {report_number(total)} users."
        else:
            self.text("No stored first-user channel data for this period.", 50, 240, color=_MUTED)
            note = "GA4 channel detail is unavailable. Search Console separately measures clicks from Google Search."
        top = 235 + channel_height
        if self.show_comparison:
            top = self.note("What this means", note, _MARGIN, top) + 12
        right = _MARGIN + _HALF + _GAP
        engagement_end = self.note("Visitor engagement", "Engagement rate, average engagement time, and pages per session are unavailable.",
                                   _MARGIN, top, _HALF, color=white)
        visitors_end = self.note("Who visits", "Device split, top locations, and new vs. returning users are unavailable.",
                                 right, top, _HALF, color=white)
        top = max(engagement_end, visitors_end) + 12
        self.chart("Daily GA4 users", self.series("ga4_daily", "totalUsers"), _MARGIN, top, _HALF, 139, dots=True)
        self.chart("Daily GA4 page views", self.series("ga4_daily", "screenPageViews"),
                   right, top, _HALF, 139, color=_BLUE, dots=True)

    def rankings(self):
        page_table = self.data_table("top_pages", _WIDTH)
        query_table = self.data_table("top_queries", _WIDTH)
        keyword_columns, keyword_rows = self.keyword_data()
        if self.weekly and page_table is None and query_table is None and not keyword_rows:
            return None
        subtitle = f"{self.period_label} Search Console results."
        if keyword_rows:
            subtitle = f"{self.period_label} Search Console results and keyword data from the linked sheet."
        self.page("SEARCH VISIBILITY", "Rankings and top pages",
                  subtitle)
        top = 176
        if page_table is not None or not self.weekly:
            top = self.table_section("Top performing pages", page_table, top, "Selected pages, ordered by clicks.")
        pages = self.tables.get("top_pages", {}).get("rows", [])
        if self.show_comparison and page_table is not None:
            best = max(pages, key=lambda row: row.get("impressions") or 0)
            if top > 690:
                self.page(*self.section)
                top = 176
            page = str(best.get("page") or "The highest-visibility page")
            if len(page) > 225:
                page = page[:222] + "..."
            detail = (f"{page} recorded {_metric('impressions', best.get('impressions'))} impressions and "
                      f"{_metric('clicks', best.get('clicks'))} clicks among the pages shown.")
            top = self.note("Highest-visibility page to review", detail, _MARGIN, top, minimum=57) + 18
        if query_table is not None or not self.weekly:
            top = self.table_section("What people searched", query_table, top,
                                     "Selected Search Console queries from the stored report.")
        if not keyword_rows:
            return top
        count = len(keyword_rows)
        spreadsheet_id = self.dashboard.get("sources", {}).get("google_sheets", {}).get("spreadsheet_id")
        link = None
        limit = 20
        if count > 10 and isinstance(spreadsheet_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
            link = ("View all keywords", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")
            limit = 10
        title = "Targeted Keyword Positioning"
        description = "Current keyword snapshot from the linked sheet."
        if count > limit:
            description = f"Showing the first {limit} rows from the linked sheet."
        values = [[_metric(column, row.get(column)) for column in keyword_columns] for row in keyword_rows[:limit]]
        table = self.table(keyword_columns, values, _WIDTH)
        return self.table_section(title, table, top, description, link=link)

    def page_speed_audits(self, google_cloud_console_key):
        strategies = ("mobile", "desktop")
        with ThreadPoolExecutor(max_workers=2) as pool:
            audits = {strategy: pool.submit(pagespeed_audit, self.site_url, strategy=strategy, api_key=google_cloud_console_key)
                      for strategy in strategies}
        return {strategy: audit.result() for strategy, audit in audits.items()}

    def page_speed(self, results, top):
        title = "PageSpeed Insights"
        subtitle = f"{self.site or 'Website'} / Lab performance measurements taken when the report is generated."
        height = 192 if any(audit is not None for audit, _ in results.values()) else 85
        subtitle_height = self.paragraph(subtitle, size=9).wrap(_WIDTH, _PAGE_HEIGHT)[1]
        if self.weekly and top is not None and top + 32 + subtitle_height + height <= _BOTTOM:
            top += self.text(title, _MARGIN, top, size=15, bold=True) + 5
            top += self.text(subtitle, _MARGIN, top, color=_MUTED) + 8
        else:
            self.page("SITE PERFORMANCE", title, subtitle)
            top = 176
        for i, (strategy, result) in enumerate(results.items()):
            x = _MARGIN + i * (_HALF + _GAP)
            self.pagespeed_card(x, top, height, strategy.title(), *result)
        return top + height + 22

    def pagespeed_card(self, x, top, height, title, audit, error):
        self.box(x, top, _HALF, height, border=True)
        self.text(title, x + 12, top + 12, _HALF - 24, size=15, bold=True, color=_TEAL)
        if audit is None:
            self.text(error, x + 12, top + 38, _HALF - 24, color=_MUTED)
            return
        tested_url, timestamp, metrics = audit
        audited_on = _date(timestamp.partition("T")[0])
        label = f"{audited_on:%d %B %Y} / PageSpeed Insights" if audited_on else "PageSpeed Insights"
        self.text(label, x + 12, top + 38, _HALF - 24, size=8.5, color=_MUTED)
        for i, (name, value) in enumerate(metrics):
            row_top = top + 58 + 25 * i
            self.line(x + 12, row_top, x + _HALF - 12)
            self.text(name, x + 12, row_top + 7, _HALF - 82, size=9)
            self.canvas.setFont(_BOLD, 9)
            self.canvas.setFillColor(_TEAL)
            self.canvas.drawRightString(x + _HALF - 12, _PAGE_HEIGHT - row_top - 15, value)
        self.text(tested_url, x + 12, top + 158, _HALF - 24, size=7.5, color=_MUTED)

    def planning(self, plans, top):
        if not plans:
            return top
        title = f"Next {self.period_unit.title()} Plan"
        subtitle = f"Priorities and planned SEO activities after {self.period_label}."
        checklist = f"Next {self.period_unit} checklist"
        first_part = next(plan_parts(self.paragraph(plans[0], size=9.8, bold=True), _WIDTH - 51))
        first_height = first_part.wrap(_WIDTH - 51, _PAGE_HEIGHT)[1]
        if top + 44 + max(35, first_height + 20) > _BOTTOM:
            self.page("PLANNING", title, subtitle)
            top = 176
        else:
            top += self.text(title, _MARGIN, top, size=15, bold=True) + 5
        top += self.text(checklist, _MARGIN, top, size=9, color=_MUTED) + 9
        for plan in plans:
            paragraph = self.paragraph(plan, size=9.8, bold=True)
            for part in plan_parts(paragraph, _WIDTH - 51):
                _, text_height = part.wrap(_WIDTH - 51, _PAGE_HEIGHT)
                height = max(35, text_height + 20)
                if top + height > _BOTTOM:
                    self.page("PLANNING", title, subtitle)
                    top = 178
                self.box(_MARGIN, top, _WIDTH, height, border=True)
                self.canvas.setStrokeColor(_TEAL)
                self.canvas.roundRect(_MARGIN + 12, _PAGE_HEIGHT - top - 24, 10, 10, 2, fill=0, stroke=1)
                part.drawOn(self.canvas, _MARGIN + 32, _PAGE_HEIGHT - top - 13 - text_height)
                top += height + 7
        return top + 11

    def appendix(self, top):
        title = "Daily source data"
        subtitle = f"{self.period_label} daily figures from the stored source reports."
        right = _MARGIN + _HALF + _GAP
        sections = [("Google Search Console", self.data_table("gsc_daily", _HALF, compact=True)),
                    ("Google Analytics 4", self.data_table("ga4_daily", _HALF, compact=True))]
        sessions = self.series("ga4_daily", "sessions")
        heights = [table.wrap(_HALF, _PAGE_HEIGHT)[1] if table is not None else 12 for _, table in sections]
        if sessions:
            heights[1] += 132
        if self.weekly and top + max(heights) + 112 <= _BOTTOM:
            top += self.text(title, _MARGIN, top, size=15, bold=True) + 5
            top += self.text(subtitle, _MARGIN, top, color=_MUTED) + 12
        else:
            self.page("APPENDIX", title, subtitle)
            top = 176
        while True:
            remaining = []
            ends = []
            for (source, table), x in zip(sections, (_MARGIN, right)):
                self.text(source, x, top, _HALF, size=12, bold=True)
                table_space = min(508, _BOTTOM - top - 22)
                if table is not None and table.wrap(_HALF, table_space)[1] > table_space:
                    pieces = table.split(_HALF, table_space)
                    if len(pieces) != 2:
                        raise ValueError(f"Daily data cannot fit on the page: {source}")
                    table, rest = pieces
                else:
                    rest = None
                ends.append(self.draw_table(table, x, top + 22, _HALF))
                remaining.append((source, rest))
            if not any(table is not None for _, table in remaining):
                break
            sections = remaining
            self.page(*self.section)
            top = 176
        top = max(ends)
        if sessions:
            chart_top = ends[1] + 12
            chart_height = min(120, _BOTTOM - chart_top - 52)
            if chart_height < 80:
                self.page(*self.section)
                self.chart("Daily GA4 sessions", sessions, _MARGIN, 176, _WIDTH, 160, dots=True)
                top = 336
            else:
                self.chart("Daily GA4 sessions", sessions, right, chart_top, _HALF, chart_height, dots=True)
                top = max(top, chart_top + chart_height)
        top += 12
        top += self.text("A dash indicates unavailable data. Counts are shown as reported by each source.",
                         _MARGIN, top, color=_MUTED) + 9
        sources = ["Google Search Console", "Google Analytics 4"]
        if self.keyword_data()[1]:
            sources.append("the linked keyword sheet")
        sources.append("PageSpeed Insights")
        self.text(f"Sources: {', '.join(sources)}.", _MARGIN, top, color=_MUTED)

    def change_insights(self, improvements, declines):
        if not improvements and not declines:
            return
        self.page("PERFORMANCE", "Performance insights", "Verified changes and recommended next steps for the website.")
        top = 176
        for heading, signals, color in (("Improvement opportunities", declines, _SLATE_BLUE),
                                        ("Positive signals", improvements, _INSIGHT_GREEN)):
            if not signals:
                continue
            if top + 107 > _BOTTOM:
                self.page(*self.section)
                top = 176
            top += self.text(heading, _MARGIN, top, size=15, bold=True) + 10
            for signal in signals:
                if top + 80 > _BOTTOM:
                    self.page(*self.section)
                    top = 176
                top = self.note(signal["title"], _signal_detail(signal), _MARGIN, top,
                                color=color, text_color=white) + 12


def plan_parts(paragraph, width):
    pending = [paragraph]
    while pending:
        part = pending.pop()
        if part.wrap(width, 460)[1] <= 460:
            yield part
        else:
            pending.extend(reversed(part.split(width, 460)))


def generate_analytics_pdf(
    dashboard: dict[str, Any],
    period: Literal["monthly", "weekly"],
    client_name: str,
    *,
    comparison: bool = False,
    next_plans: list[str] | None = None,
    openai_api_key: str | None = None,
    google_cloud_console_key: str | None = None,
) -> bytes:
    output = BytesIO()
    report = _PeriodReport(output, dashboard, period, client_name, comparison)
    plans = [plan.strip() for plan in next_plans or () if plan.strip()]
    signals = (
        report_signals(dashboard.get("kpis", []), False, openai_api_key, include_declines=True)
        if report.show_comparison else []
    )
    improvements = [signal for signal in signals if signal["direction"] == "up"]
    declines = [signal for signal in signals if signal["direction"] == "down"]
    audits = report.page_speed_audits(google_cloud_console_key)
    shown = report.cover(improvements, declines, plans, audits)
    report.performance()
    report.change_insights(improvements[shown:], declines)
    report.audience()
    rankings_end = report.rankings()
    planning_top = report.page_speed(audits, rankings_end)
    appendix_top = report.planning(plans, planning_top)
    report.appendix(appendix_top)
    report.finish()
    return output.getvalue()

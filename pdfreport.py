"""Pure-stdlib PDF chart renderer for sargeant.

Turns a parsed sar report (the dict shape produced by ``parser.SarReport
.to_dict()``) into a multi-page PDF of vector time-series charts — the same
series the browser UI draws with uPlot.

Why hand-rolled: sargeant's covenant is "no third-party dependencies, runs
offline" (see README and project memory ``sargeant-stack``), and the Python
standard library has no PDF writer. The subset of PDF needed for line charts
and text is small and stable, though: a handful of numbered objects, a
cross-reference table, Flate (zlib) content streams, the base-14 Helvetica
fonts (built into every PDF viewer, nothing to embed), and a few drawing
operators (``m``/``l``/``S`` paths, ``re`` rects, ``BT``/``Tj``/``ET`` text).
That subset is exactly what this module implements — nothing more.

Reference: PDF 1.7 (ISO 32000-1) — §7 file structure, §8 graphics, §9 text.

Entry point: :func:`render_pdf`. Both the CLI (``server.py --output``) and the
HTTP endpoint (``GET /api/export``) call it; neither renders any other way.
Everything here consumes the report *dict* read-only and shares no state, so
it is safe to call from concurrent request threads.
"""

from __future__ import annotations

import math
import zlib
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

RGB = tuple[float, float, float]


class ExportError(ValueError):
    """A user-correctable export problem (bad metric, no data, …)."""


# Layout constants & palette
# US Letter landscape, in points. Two chart slots per page reproduce roughly
# the aspect ratio of the on-screen cards (220px tall, container wide).
PAGE_W, PAGE_H = 792.0, 612.0
MARGIN = 40.0
SLOT_H = 232.0
SLOT_GAP = 12.0
HEADER_H = 50.0  # page-1 report banner block

MAX_METRICS = 64  # sanity bound; also caps hosted-mode request cost

# Mirrors PALETTE in static/app.js so exported charts match the UI's colors.
PALETTE: list[RGB] = [
    (0.000, 0.400, 0.800),  # #06c
    (0.914, 0.329, 0.125),  # #e95420
    (0.055, 0.518, 0.125),  # #0e8420
    (0.780, 0.086, 0.169),  # #c7162b
    (0.976, 0.608, 0.067),  # #f99b11
    (0.478, 0.176, 0.753),  # #7a2dc0
    (0.000, 0.639, 0.639),  # #00a3a3
    (0.702, 0.294, 0.000),  # #b34b00
    (0.361, 0.361, 0.361),  # #5c5c5c
    (0.165, 0.490, 0.882),  # #2a7de1
]

_GRID = (0.90, 0.90, 0.90)
_BORDER = (0.62, 0.62, 0.62)
_INK = (0.13, 0.13, 0.13)
_MUTED = (0.42, 0.42, 0.42)

# Text metrics & string escaping
# Helvetica glyph widths in 1/1000 em. Only digits need to be exact (they
# right-align the y-axis tick labels, and every Helvetica digit is 556); the
# rest are the standard AFM widths for common glyphs and a close default,
# which is plenty to right-align and center labels.
_WIDTHS: dict[str, int] = {
    ch: width
    for chars, width in (
        ("0123456789#$_?abdeghnopqu", 556),
        (" .,:;!/'ftIij", 278),
        ("()[]-r·", 333),
        ("cksvxyzJ", 500),
        ("FTZ", 611),
        ("ABEKPSVXY&", 667),
        ("CDHNRU", 722),
        ("GOQ", 778),
        ("Mm", 833),
        ("Ww", 944),
        ("%", 889),
        ("l", 222),
        ("+=<>", 584),
    )
    for ch in chars
}

# PDF literal strings delimit with parentheses and escape with backslashes;
# control characters have no business in a label. Column names and host labels
# come from parsed (in hosted mode: untrusted) sar files, so this table is
# load-bearing, not cosmetic.
_STRING_ESCAPES = {ord(ch): f"\\{ch}" for ch in "\\()"}
_STRING_ESCAPES |= dict.fromkeys(range(32), " ")


def _text_w(s: str, size: float) -> float:
    return sum(_WIDTHS.get(ch, 556) for ch in s) * size / 1000.0


def _esc(s: str) -> str:
    """Escape a string for embedding in a PDF literal ``( … )``."""
    return s.translate(_STRING_ESCAPES)


# Low-level PDF assembly
class _Canvas:
    """Accumulates content-stream operators for one page.

    Coordinates are PDF-native: origin bottom-left, y grows upward, 72 pt/inch.
    """

    def __init__(self) -> None:
        self._ops: list[str] = []

    def _op(self, s: str) -> None:
        self._ops.append(s)

    def save(self) -> None:
        self._op("q")

    def restore(self) -> None:
        self._op("Q")

    def clip_rect(self, x: float, y: float, w: float, h: float) -> None:
        self._op(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re W n")

    def line_width(self, w: float) -> None:
        self._op(f"{w:.2f} w")

    def stroke_color(self, rgb: RGB) -> None:
        self._op(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} RG")

    def fill_color(self, rgb: RGB) -> None:
        self._op(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg")

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self._op(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def polyline(self, pts: list[tuple[float, float]]) -> None:
        if len(pts) < 2:
            return
        (x0, y0), rest = pts[0], pts[1:]
        moves = " ".join(f"{x:.2f} {y:.2f} l" for x, y in rest)
        self._op(f"{x0:.2f} {y0:.2f} m {moves} S")

    def rect(self, x: float, y: float, w: float, h: float, fill: bool = False) -> None:
        self._op(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {'f' if fill else 'S'}")

    def text(
        self,
        x: float,
        y: float,
        s: str,
        size: float = 8.0,
        bold: bool = False,
        rgb: RGB = _INK,
        align: Literal["left", "right", "center"] = "left",
    ) -> None:
        if align == "right":
            x -= _text_w(s, size)
        elif align == "center":
            x -= _text_w(s, size) / 2.0
        font = "/F2" if bold else "/F1"
        self.fill_color(rgb)
        self._op(f"BT {font} {size:.1f} Tf {x:.2f} {y:.2f} Td ({_esc(s)}) Tj ET")

    def data(self) -> bytes:
        # The fonts declare /WinAnsiEncoding, which is CP1252 — that is what
        # gives us em-dashes and middle dots; replace anything outside it.
        return "\n".join(self._ops).encode("cp1252", "replace")


def _serialize(pages: list[_Canvas]) -> bytes:
    """Assemble page canvases into a complete PDF file.

    Object numbering is fixed: 1 catalog, 2 page tree, 3/4 the two fonts,
    then (content, page) pairs. The xref table must give the exact byte
    offset of every object — viewers actually check — hence the offsets
    recorded as bodies are emitted.
    """
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
    bodies: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",  # 1
        b"",  # 2 — page tree, filled in below once kid ids are known
        font % b"Helvetica",  # 3
        font % b"Helvetica-Bold",  # 4
    ]
    kids = []
    for cv in pages:
        stream = zlib.compress(cv.data(), 6)
        bodies.append(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream"
            % (len(stream), stream)
        )
        content_id = len(bodies)
        bodies.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            b"/Contents %d 0 R >>" % (int(PAGE_W), int(PAGE_H), content_id)
        )
        kids.append(len(bodies))
    bodies[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for num, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num
        out += body
        out += b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(bodies) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref_at,
    )
    return bytes(out)


# Report-side helpers (ports of the equivalent logic in static/app.js, so the
# PDF and the UI agree about series building, rollover, and default entities)
def _hms_to_seconds(t: str) -> int:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _fmt_hm(secs: float) -> str:
    """Seconds-since-day0 -> 'HH:MM'. Like the UI, the midnight rollover shows
    as 24:00 rather than wrapping, so the axis stays monotonic to the eye."""
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    return f"{h:02d}:{m:02d}"


def _fmt_daytime(day0: str | None, secs: float) -> str:
    """Seconds-since-day0 -> 'YYYY-MM-DD HH:MM' (date-aware across the
    rollover) or plain 'HH:MM' when the report has no date."""
    hm = _fmt_hm(((secs % 86400) + 86400) % 86400)
    if not day0:
        return _fmt_hm(secs)
    try:
        d = date.fromisoformat(day0) + timedelta(days=int(secs // 86400))
    except ValueError:
        return _fmt_hm(secs)
    return f"{d.isoformat()} {hm}"


def _build_series(
    sec: dict, col_idx: int, entities: list[str]
) -> tuple[list[int], list[list[float | None]], list[str]]:
    """Port of app.js buildSeriesData(): x = union of timestamps in file
    order with a day offset added when the clock rolls past midnight; one
    y-series per selected entity (or a single series for un-keyed sections)."""
    wanted = entities if sec.get("key") else [""]
    t_index: dict[str, int] = {}
    xs: list[int] = []
    day_offset = 0
    prev = -1
    for row in sec["rows"]:
        t = row["t"]
        if t in t_index:
            continue
        secs = _hms_to_seconds(t)
        if secs < prev:
            day_offset += 86400
        prev = secs
        t_index[t] = len(xs)
        xs.append(secs + day_offset)

    series: list[list[float | None]] = [[None] * len(xs) for _ in wanted]
    pos = {e: i for i, e in enumerate(wanted)}
    for row in sec["rows"]:
        ent = row["e"] if sec.get("key") else ""
        i = pos.get(ent)
        if i is None:
            continue
        vals = row["v"]
        series[i][t_index[row["t"]]] = vals[col_idx] if col_idx < len(vals) else None
    return xs, series, list(wanted)


def _default_entities(sec: dict) -> list[str]:
    """Same defaults as the UI: 'all' for CPU-style sections, else the first
    few entities so charts stay legible out of the box."""
    ents = sec.get("entities") or []
    if not sec.get("key"):
        return [""]
    if "all" in ents:
        return ["all"]
    return ents[:4]


def _pick_entities(sec: dict, requested: list[str] | None) -> list[str]:
    if not sec.get("key"):
        return [""]
    if requested:
        avail = set(sec.get("entities") or [])
        if selected := [e for e in requested if e in avail]:
            return selected
    return _default_entities(sec)


# Metric resolution — a spec is a bare column name ("tps") or KEY:column
# ("DEV:tps") when the bare name is ambiguous across sections
def _resolve_metric(report: dict, spec: str) -> tuple[dict, int]:
    """Map 'colname' or 'KEY:colname' to (section, column index).

    A bare name that appears in several sections resolves to the un-keyed one
    when that is unique (so 'tps' means the system-wide I/O section, not the
    per-DEV one); otherwise the caller must qualify it.
    """
    key = None
    col = spec
    if ":" in spec:
        key, col = spec.split(":", 1)
    matches = [
        (sec, sec["columns"].index(col))
        for sec in report.get("sections", [])
        if col in sec["columns"] and (key is None or sec.get("key") == key)
    ]
    if not matches:
        raise ExportError(f"unknown metric {spec!r}{_suggest(report, col)}")
    if len(matches) == 1:
        return matches[0]
    unkeyed = [m for m in matches if not m[0].get("key")]
    if key is None and len(unkeyed) == 1:
        return unkeyed[0]
    alts = ", ".join(
        f"{sec['key']}:{col}" if sec.get("key") else col for sec, _ in matches
    )
    raise ExportError(f"ambiguous metric {col!r} — qualify it as one of: {alts}")


def _suggest(report: dict, col: str) -> str:
    cols = dict.fromkeys(
        c for sec in report.get("sections", []) for c in sec["columns"]
    )
    low = col.lower()
    near = [c for c in cols if low in c.lower() or c.lower() in low]
    return f" — did you mean: {', '.join(near[:6])}?" if near else ""


def format_available(report: dict) -> str:
    """A compact, human-readable listing of every exportable column, grouped
    by section, for CLI error messages."""
    lines = []
    for sec in report.get("sections", []):
        prefix = f"per {sec['key']}:  " if sec.get("key") else ""
        lines.append(f"  {prefix}{' '.join(sec['columns'])}")
    return "\n".join(lines) or "  (report has no sections)"


# Axis math
def _nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Classic 1-2-5 'nice numbers' ticks covering [lo, hi]."""
    span = hi - lo
    if span <= 0:
        return [lo]
    step = 10.0 ** math.floor(math.log10(span / target))
    for mult in (1.0, 2.0, 5.0, 10.0):
        if span / (step * mult) <= target:
            step *= mult
            break
    k0 = math.ceil(lo / step - 1e-9)
    k1 = math.floor(hi / step + 1e-9)
    return [round(k * step, 10) for k in range(k0, k1 + 1)]


_X_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def _time_ticks(x0: float, x1: float, max_ticks: int = 8) -> list[int]:
    span = max(x1 - x0, 1.0)
    step = next((s for s in _X_STEPS if span / s <= max_ticks), _X_STEPS[-1])
    k0 = math.ceil(x0 / step)
    k1 = math.floor(x1 / step)
    return [k * step for k in range(k0, k1 + 1)]


def _fmt_val(v: float) -> str:
    """Short y-tick labels: 32500000 -> '32.5M', 0.30000000004 -> '0.3'."""
    a = abs(v)
    if a >= 1e9:
        scaled, suffix = v / 1e9, "G"
    elif a >= 1e6:
        scaled, suffix = v / 1e6, "M"
    elif a >= 1e4:
        scaled, suffix = v / 1e3, "k"
    else:
        return f"{v:.6g}"
    return f"{scaled:.1f}".removesuffix(".0") + suffix


# Render the charts
@dataclass(frozen=True)
class _Chart:
    """One chart-to-be: a resolved metric with its series data and labelling."""

    title: str
    sub: str
    xs: list[int]
    series: list[list[float | None]]
    labels: list[str]
    keyed: bool


def _window(
    xs: list[int],
    series: list[list[float | None]],
    t_from: float | None,
    t_to: float | None,
) -> tuple[list[int], list[list[float | None]]]:
    """Slice the series to a [from, to] x-window (the UI's zoom). Falls back
    to the full range when the window would leave fewer than two points."""
    if t_from is None and t_to is None:
        return xs, series
    lo = t_from if t_from is not None else float("-inf")
    hi = t_to if t_to is not None else float("inf")
    i0 = bisect_left(xs, lo)
    i1 = bisect_right(xs, hi)
    if i1 - i0 < 2:
        return xs, series
    return xs[i0:i1], [s[i0:i1] for s in series]


def _draw_chart(
    cv: _Canvas, x0: float, y0: float, w: float, h: float, chart: _Chart
) -> None:
    """Draw one chart card with its slot's lower-left corner at (x0, y0)."""
    title_y = y0 + h - 10.0
    show_legend = chart.keyed and bool(chart.labels)
    pad_top = 28.0 if show_legend else 16.0
    pad_bottom = 18.0
    gut_l = 48.0
    plot_x = x0 + gut_l
    plot_y = y0 + pad_bottom
    plot_w = w - gut_l - 6.0
    plot_h = h - pad_top - pad_bottom - 12.0

    cv.text(x0, title_y, chart.title, size=10.5, bold=True)
    cv.text(x0 + w, title_y, chart.sub, size=8.0, rgb=_MUTED, align="right")

    if show_legend:
        lx = x0
        ly = y0 + h - 23.0
        shown = 0
        for i, label in enumerate(chart.labels):
            lw = _text_w(label, 7.5)
            if lx + 14 + lw > x0 + w - 60 and shown:
                remaining = len(chart.labels) - shown
                cv.text(lx, ly, f"(+{remaining} more)", size=7.5, rgb=_MUTED)
                break
            cv.stroke_color(PALETTE[i % len(PALETTE)])
            cv.line_width(1.8)
            cv.line(lx, ly + 2.6, lx + 10, ly + 2.6)
            cv.text(lx + 13, ly, label, size=7.5, rgb=_INK)
            lx += 13 + lw + 11
            shown += 1

    xs = chart.xs
    values = [v for s in chart.series for v in s if v is not None and math.isfinite(v)]
    if not xs or not values:
        cv.rect(plot_x, plot_y, plot_w, plot_h)
        cv.text(
            plot_x + plot_w / 2,
            plot_y + plot_h / 2,
            "no data in this window",
            size=8.5,
            rgb=_MUTED,
            align="center",
        )
        return

    v_min, v_max = min(values), max(values)
    if v_min == v_max:  # flat series: give the line some room
        pad = abs(v_min) * 0.1 or 1.0
    else:
        pad = (v_max - v_min) * 0.04
    lo, hi = v_min - pad, v_max + pad
    if v_min >= 0 and lo < 0:
        lo = 0.0  # don't invent a negative axis for non-negative data
    x_lo, x_hi = float(xs[0]), float(xs[-1])
    if x_lo == x_hi:
        x_lo, x_hi = x_lo - 60, x_hi + 60

    def px(x: float) -> float:
        return plot_x + (x - x_lo) / (x_hi - x_lo) * plot_w

    def py(v: float) -> float:
        return plot_y + (v - lo) / (hi - lo) * plot_h

    # Grid + tick labels, then the frame over the grid, then the series.
    cv.line_width(0.5)
    for t in _nice_ticks(lo, hi):
        if not lo <= t <= hi:
            continue
        y = py(t)
        cv.stroke_color(_GRID)
        cv.line(plot_x, y, plot_x + plot_w, y)
        cv.text(plot_x - 4, y - 2.6, _fmt_val(t), size=7.5, rgb=_MUTED, align="right")
    for t in _time_ticks(x_lo, x_hi):
        x = px(t)
        cv.stroke_color(_GRID)
        cv.line(x, plot_y, x, plot_y + plot_h)
        cv.text(x, plot_y - 11.5, _fmt_hm(t), size=7.5, rgb=_MUTED, align="center")

    cv.line_width(0.75)
    cv.stroke_color(_BORDER)
    cv.rect(plot_x, plot_y, plot_w, plot_h)

    cv.save()
    cv.clip_rect(plot_x, plot_y, plot_w, plot_h)
    for i, s in enumerate(chart.series):
        pts = [
            (px(x), py(v)) for x, v in zip(xs, s) if v is not None and math.isfinite(v)
        ]
        color = PALETTE[i % len(PALETTE)]
        if len(pts) == 1:  # lone reading: a dot-sized tick, not an invisible line
            cv.fill_color(color)
            cv.rect(pts[0][0] - 1, pts[0][1] - 1, 2, 2, fill=True)
            continue
        cv.stroke_color(color)
        cv.line_width(1.2)
        cv.polyline(pts)
    cv.restore()


# Public entry point
def render_pdf(
    report: dict,
    metrics: list[str],
    entities: list[str] | None = None,
    t_from: float | None = None,
    t_to: float | None = None,
    source_name: str = "",
) -> bytes:
    """Render the requested metric columns of ``report`` to a PDF.

    ``metrics`` are column names as printed by sar (optionally KEY-qualified,
    e.g. ``DEV:tps``); each becomes one chart. ``entities`` narrows keyed
    sections (per-CPU/DEV/IFACE…) to specific series; the default matches the
    UI. ``t_from``/``t_to`` crop to the UI's zoom window, in seconds since the
    report's first midnight. Raises :class:`ExportError` for problems the
    caller (CLI user or HTTP client) can fix.
    """
    if not metrics:
        raise ExportError("no metrics requested")
    if len(metrics) > MAX_METRICS:
        raise ExportError(f"too many metrics ({len(metrics)} > {MAX_METRICS})")

    charts = []
    for spec in metrics:
        sec, col_idx = _resolve_metric(report, spec)
        ents = _pick_entities(sec, entities)
        xs, series, labels = _build_series(sec, col_idx, ents)
        xs, series = _window(xs, series, t_from, t_to)
        keyed = bool(sec.get("key"))
        n_all = len(sec.get("entities") or [])
        charts.append(
            _Chart(
                title=sec["columns"][col_idx],
                sub=(
                    f"per {sec['key']} · {len(ents)} of {n_all} series"
                    if keyed
                    else "system-wide"
                ),
                xs=xs,
                series=series,
                labels=labels,
                keyed=keyed,
            )
        )

    # Paginate: page 1 carries the report banner, every page holds two slots.
    pages: list[_Canvas] = []
    first_top = PAGE_H - MARGIN - HEADER_H
    other_top = PAGE_H - MARGIN
    i = 0
    while i < len(charts) or not pages:
        cv = _Canvas()
        y = first_top if not pages else other_top
        pages.append(cv)
        while i < len(charts) and y - SLOT_H >= MARGIN - 2:
            _draw_chart(cv, MARGIN, y - SLOT_H, PAGE_W - 2 * MARGIN, SLOT_H, charts[i])
            y -= SLOT_H + SLOT_GAP
            i += 1

    # Page-1 banner: what machine, what day, what file.
    hdr = pages[0]
    host = report.get("host") or "unknown host"
    hdr.text(MARGIN, PAGE_H - MARGIN - 12, f"sargeant — {host}", size=13, bold=True)
    meta = " · ".join(
        part
        for part in (
            report.get("kernel"),
            report.get("arch"),
            f"{report['ncpu']} CPU" if report.get("ncpu") else None,
            report.get("day"),
            f"source {source_name}" if source_name else None,
        )
        if part
    )
    hdr.text(MARGIN, PAGE_H - MARGIN - 26, meta, size=8.5, rgb=_MUTED)
    if t_from is not None or t_to is not None:
        day0 = report.get("day")
        w0 = _fmt_daytime(day0, t_from) if t_from is not None else "start"
        w1 = _fmt_daytime(day0, t_to) if t_to is not None else "end"
        # ASCII arrow: WinAnsi has no U+2192, unlike the UI's timeline label.
        hdr.text(
            PAGE_W - MARGIN,
            PAGE_H - MARGIN - 26,
            f"window {w0} -> {w1}",
            size=8.5,
            rgb=_MUTED,
            align="right",
        )

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for n, cv in enumerate(pages, start=1):
        cv.text(MARGIN, 22, f"sargeant · generated {stamp}", size=7.5, rgb=_MUTED)
        cv.text(
            PAGE_W - MARGIN,
            22,
            f"page {n} / {len(pages)}",
            size=7.5,
            rgb=_MUTED,
            align="right",
        )
    return _serialize(pages)

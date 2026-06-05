"""Data-driven parser for sysstat `sar` *text* reports.

Design goals:
  * stdlib only — no third-party or external dependencies, portable and clean!
  * NOT a hardcoded per-section list. Sections are discovered structurally so
    that sar versions emitting new sections still render. The only thing we
    special case here is the well-known set of 'entity key' column names (CPU, DEV,
    IFACE) which mark a section as having multiple series per timestamp.

A sar text file looks like::

    Linux 7.0.0-15-generic (host)   2026-05-25  _x86_64_  (8 CPU)

    00:00:11   CPU   %usr   %nice   ...
    00:10:06   all   0.60   0.00    ...
    00:10:06     0   0.53   0.00    ...
    ...
    <blank line>
    00:00:11   kbmemfree  kbavail  ...
    ...

Sections are separated by blank lines; the first line after a blank line is a
'header' row whose columns (after the timestamp) name the metrics.

References
----------
The sar text layout and per-section field semantics this parser targets are
defined by the sysstat suite (Sebastien Godard):

  * sysstat project & source ... https://github.com/sysstat/sysstat
  * sysstat home page .......... https://sysstat.github.io/
  * sar(1) man page ............ https://man7.org/linux/man-pages/man1/sar.1.html
  * sadf(1) man page ........... https://man7.org/linux/man-pages/man1/sadf.1.html

sar(1) documents the report layout and the metric columns of each activity
section; sadf(1) covers sysstat's own machine-readable renderings (CSV/XML/JSON)
of that same data, a useful cross-check for how a section is structured.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date

# Column names that, when they appear as the first metric column of a section,
# mean each timestamp carries one row *per entity* (a CPU id, a disk, a NIC).
# Matching is case-sensitive against the raw sar header token.
ENTITY_KEYS = {
    "CPU",
    "DEV",
    "IFACE",
    "TTY",
    "FILESYSTEM",
    "BUS",
    "FAN",
    "TEMP",
    "IN",
    "DEVICE",
    "MHz",
}

_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")
# sar sometimes emits 12-hour clocks with an AM/PM column.
_AMPM = {"AM", "PM"}

# Defensive ceilings for parsing untrusted uploads. A crafted file can
# otherwise make us build an unbounded in-memory structure (and a huge JSON
# response). These are deliberately generous for real sar files (a full day is
# typically a few tens of thousands of rows) but bound the worst case. Override
# per-call if you trust the source (the offline single-user mode passes None).
DEFAULT_MAX_SECTIONS = 2_000
DEFAULT_MAX_ROWS = 2_000_000


@dataclass
class Section:
    """One sar section: a metric group sharing a column layout."""

    name: str  # human label, derived from columns
    columns: list[str]  # metric column names (excludes time + key)
    key: str | None  # entity-key column name, or None
    entities: list[str] = field(default_factory=list)
    # rows: list of {"t": "HH:MM:SS", "e": <entity or "">, "v": [floats]}
    rows: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SarReport:
    host: str
    kernel: str
    arch: str
    ncpu: int | None
    day: str | None  # ISO date string from the header line
    sections: list[Section] = field(default_factory=list)
    truncated: bool = False  # True if parse hit a defensive ceiling

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "kernel": self.kernel,
            "arch": self.arch,
            "ncpu": self.ncpu,
            "day": self.day,
            "truncated": self.truncated,
            "sections": [s.to_dict() for s in self.sections],
        }


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _parse_header_line(line: str) -> tuple[str, str, str, int | None, str | None]:
    """Parse the top banner line.

    e.g. ``Linux 7.0.0-15-generic (db-prod-07)  2026-05-25  _x86_64_  (8 CPU)``
    Fields are whitespace/tab separated and somewhat loose, so parse defensively.
    """
    kernel = host = arch = ""
    ncpu = None
    day = None

    m = re.search(r"\(([^)]*)\)\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{2,4})", line)
    if m:
        host = m.group(1)
        raw_day = m.group(2)
        day = _normalize_day(raw_day)

    m = re.match(r"^(\S+)\s+(\S+)", line)
    if m:
        kernel = m.group(2)

    m = re.search(r"_(\w+)_", line)
    if m:
        arch = m.group(1)

    m = re.search(r"\((\d+)\s+CPU\)", line)
    if m:
        ncpu = int(m.group(1))

    return host, kernel, arch, ncpu, day


def _normalize_day(raw: str) -> str | None:
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    m = re.match(r"^(\d{2})/(\d{2})/(\d{2,4})$", raw)
    if m:
        mm, dd, yy = m.groups()
        if len(yy) == 2:
            yy = "20" + yy
        try:
            return date(int(yy), int(mm), int(dd)).isoformat()
        except ValueError:
            return None
    return None


# Proactive hostname scrubbing (PII)
# In a customer's sosreport the machine name in the sar banner can be PII. It
# appears in exactly one place: the ``(HOST)`` token that precedes the date on
# the first banner line, e.g.
#   Linux 7.0.0-15-generic (db-prod-07)   2026-05-25  _x86_64_  (8 CPU)
# We rewrite just that token (not the trailing ``(8 CPU)``) before the upload is
# ever stored or parsed, so neither the file on disk nor the served JSON leaks
# it. Bytes in/out so the caller needn't decode the whole upload.
_BANNER_HOST_RE = re.compile(
    rb"\(([^)]*)\)(\s+(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{2,4}))"
)


def random_host_label() -> str:
    return "host-" + secrets.token_hex(4)


def scrub_banner_hostname(data: bytes, label: str | None = None) -> bytes:
    """Replace the hostname on the sar banner (first line only) with ``label``
    (a fresh random label if not given). Returns the data unchanged if no
    banner hostname is found."""
    repl = (label or random_host_label()).encode()
    nl = data.find(b"\n")
    first = data if nl == -1 else data[:nl]
    rest = b"" if nl == -1 else data[nl:]
    new_first = _BANNER_HOST_RE.sub(
        lambda m: b"(" + repl + b")" + m.group(2), first, count=1
    )
    return new_first + rest


def _split_header(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Given metric tokens (after the timestamp), return (key, columns)."""
    if tokens and tokens[0] in ENTITY_KEYS:
        return tokens[0], tokens[1:]
    return None, tokens


def _section_name(key: str | None, columns: list[str]) -> str:
    base = columns[0] if columns else "section"
    if key:
        return f"{key}: {base}…"
    return base


def parse_text(
    text: str,
    max_sections: int | None = DEFAULT_MAX_SECTIONS,
    max_rows: int | None = DEFAULT_MAX_ROWS,
) -> SarReport:
    """Parse sar text into a :class:`SarReport`.

    ``max_sections`` / ``max_rows`` bound the in-memory result when parsing
    untrusted input; pass ``None`` for either to disable that ceiling (the
    offline single-user mode trusts its local files and does so). When a ceiling
    is hit, parsing stops early and ``report.truncated`` is set.
    """
    lines = text.splitlines()
    total_rows = 0

    host = kernel = arch = ""
    ncpu = None
    day = None

    # The banner is the first non-empty line
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines):
        host, kernel, arch, ncpu, day = _parse_header_line(lines[idx])
        idx += 1

    report = SarReport(host=host, kernel=kernel, arch=arch, ncpu=ncpu, day=day)

    # Group lines into blank-line-delimited blocks; each block is one section
    # (possibly repeated across the file when sar re-prints "Average:" etc.).
    current: Section | None = None
    sections_by_sig: dict[tuple, Section] = {}

    def flush_new_header(tokens_after_time: list[str]) -> Section:
        key, columns = _split_header(tokens_after_time)
        sig = (key, tuple(columns))
        sec = sections_by_sig.get(sig)
        if sec is None:
            if max_sections is not None and len(report.sections) >= max_sections:
                report.truncated = True
                return None
            sec = Section(name=_section_name(key, columns), columns=columns, key=key)
            sections_by_sig[sig] = sec
            report.sections.append(sec)
        return sec

    prev_blank = True  # so the first content line is treated as a header
    for line in lines[idx:]:
        if not line.strip():
            prev_blank = True
            current = None
            continue

        tokens = line.split()
        # Drop a leading AM/PM marker that follows the timestamp on 12h clocks
        time_tok = tokens[0]
        rest = tokens[1:]
        if rest and rest[0] in _AMPM:
            rest = rest[1:]

        is_time = bool(_TIME_RE.match(time_tok))

        if prev_blank:
            # First line of a block. If it starts with a timestamp it is a real
            # section header; otherwise it is noise (a repeated banner, etc.).
            # A header whose first metric column looks numeric is unusual but we
            # still treat its tokens as columns.
            prev_blank = False
            current = flush_new_header(rest) if (is_time and rest) else None
            continue

        if current is None or not is_time:
            continue

        # Data row
        entity = ""
        values = rest
        if current.key is not None:
            if not values:
                continue
            entity = values[0]
            values = values[1:]
            if entity not in current.entities:
                current.entities.append(entity)

        # Skip "Average:" style summary rows (time_tok wouldn't match _TIME_RE,
        # so they're already excluded). Coerce values to float where possible
        vals: list[float | None] = []
        for v in values[: len(current.columns)]:
            vals.append(float(v) if _is_number(v) else None)
        # pad short rows
        while len(vals) < len(current.columns):
            vals.append(None)

        current.rows.append({"t": time_tok, "e": entity, "v": vals})
        total_rows += 1
        if max_rows is not None and total_rows >= max_rows:
            report.truncated = True
            break

    return report


def parse_file(
    path: str,
    max_sections: int | None = DEFAULT_MAX_SECTIONS,
    max_rows: int | None = DEFAULT_MAX_ROWS,
) -> SarReport:
    with open(path, "r", errors="replace") as fh:
        return parse_text(fh.read(), max_sections=max_sections, max_rows=max_rows)


if __name__ == "__main__":
    import json
    import sys

    rep = parse_file(sys.argv[1])
    out = rep.to_dict()
    # Summarize unless --full is passed in
    if "--full" not in sys.argv:
        for s in out["sections"]:
            print(
                f"{s['name']:<28} key={s['key']!s:<6} "
                f"cols={len(s['columns'])} entities={len(s['entities'])} "
                f"rows={len(s['rows'])}"
            )
        print(
            f"\n{len(out['sections'])} sections | host={out['host']} "
            f"day={out['day']} ncpu={out['ncpu']}"
        )
    else:
        json.dump(out, sys.stdout, indent=2)

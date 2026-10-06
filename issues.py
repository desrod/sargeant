"""Checks behind sargeant's "Issues detected" banner.

analyze() reads a parsed report dict (parser.SarReport.to_dict()) and returns
findings for the UI banner and the PDF export: a severity, a title, the time
windows in chart x units, an explanation, and a next step.

Each check has guards because a plain threshold misleads on real sar data:
%util reads 100% on disks that aren't saturated, and sar prints since-boot
totals as one sample's rate when an interface reappears. A check whose
columns aren't in the file is reported as not checked, never as healthy.

Finding text is also drawn in PDFs, whose built-in fonts only cover cp1252,
so it avoids symbols such as ≥ and ≈.
"""

from __future__ import annotations

import parser as sar
import re
import statistics
import traceback

SEVERITY_ORDER = {"crit": 0, "warn": 1, "info": 2}
GIB = 1048576  # in kB, sar's memory unit

# sar names disks devMAJOR-MINOR unless run with -p; RAM disks, loop devices
# and CD-ROMs have the fixed majors 1, 7 and 11.
_SKIP_DISK = re.compile(r"^(dev(1|7|11)-\d+|ram\d|zram\d|loop\d|sr\d)")
_VIRTUAL_IFACE = re.compile(
    r"^(lo|veth|cali|docker|br-|virbr|tunl|tun\d|tap|vnet|flannel|cni|vxlan|"
    r"kube|weave|cilium|lxc|nodelocaldns|dummy|ifb|wg|genev|ovs|egress)"
)
_BOND = re.compile(r"^(bond|team)")


def _section(report: dict, column: str, key: str | None = None):
    """(index, section) of the first section with this column and entity key."""
    for i, sec in enumerate(report["sections"]):
        if column in sec["columns"] and sec.get("key") == key:
            return i, sec
    return None, None


def _table(sec: dict, columns: list[str]):
    """The section's x axis and, per entity, the named columns aligned to it
    (None where that entity has no row), built in one pass over the rows."""
    xs, pos = sar.time_axis(sec)
    idx = {c: sec["columns"].index(c) for c in columns if c in sec["columns"]}
    table: dict[str, dict[str, list]] = {}
    for row in sec["rows"]:
        cols = table.get(row["e"])
        if cols is None:
            cols = table[row["e"]] = {c: [None] * len(xs) for c in idx}
        i, vals = pos[row["t"]], row["v"]
        for c, k in idx.items():
            if k < len(vals):
                cols[c][i] = vals[k]
    return xs, table


def _interval(xs: list[int]) -> float | None:
    steps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
    return statistics.median(steps) if steps else None


def _windows(xs: list[int], flags: list[bool], min_dur: float, breaks=()):
    """Sustained runs of flagged samples, as (start, end, sample indexes).

    sar stamps a row at the end of its interval, so a run starts one interval
    before its first sample. A run bridges up to 3 minutes of unflagged
    samples, splits at sampling gaps and restarts, and is kept only when its
    flagged samples cover min_dur seconds.
    """
    iv = _interval(xs)
    if not iv:
        return []
    runs, cur = [], []
    for i, x in enumerate(xs):
        if cur and (
            x - xs[i - 1] > 2.5 * iv
            or x - xs[cur[-1]] > iv + 180
            or any(xs[i - 1] < b <= x for b in breaks)
        ):
            runs.append(cur)
            cur = []
        if flags[i]:
            cur.append(i)
    if cur:
        runs.append(cur)
    return [(xs[r[0]] - iv, xs[r[-1]], r) for r in runs if len(r) * iv >= min_dur]


def _hm(x: float) -> str:
    return f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}"


def _when(windows) -> str:
    if not windows:
        return "throughout"
    span = f"{_hm(windows[0][0])}–{_hm(windows[-1][1])}"
    return span if len(windows) == 1 else f"{span}, {len(windows)} windows"


def _count(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f} M"
    return f"{n / 1e3:.0f} k" if n >= 1e3 else f"{n:.0f}"


def _restarts(report: dict) -> list[int]:
    events = report.get("events", [])
    return [sar.hms_to_seconds(e["t"]) for e in events if e["type"] == "restart"]


def _finding(severity, title, detail, next_step, where, windows=()):
    section, column, entities = where
    return {
        "severity": severity,
        "title": title,
        "when": _when(windows),
        "detail": detail,
        "next": next_step,
        "section": section,
        "column": column,
        "entities": entities,
        "windows": [[w[0], w[1]] for w in windows],
    }


def _check_reboots(report: dict) -> list[dict] | None:
    mi, mem = _section(report, "kbcached")
    ci, cpu = _section(report, "%idle", "CPU")
    if mem is None and cpu is None:
        return None
    xs, table = _table(mem or cpu, ["kbcached", "kbmemused"])
    m = table.get("", {})
    iv = _interval(xs) or 0
    where = (mi, "kbcached", []) if mem else (ci, "%usr", ["all"])
    findings = []
    for t in (e["t"] for e in report.get("events", []) if e["type"] == "restart"):
        xr = sar.hms_to_seconds(t)
        p = max((k for k, x in enumerate(xs) if x < xr), default=None)
        n = next((k for k, x in enumerate(xs) if x > xr), None)
        if p is None:
            title = f"Host booted at {_hm(xr)}"
            detail = (
                "This file begins with the boot record, so the data starts at boot."
            )
            next_step = (
                "Nothing to do unless the boot was unexpected: "
                "check journalctl --list-boots."
            )
            wins = [(xr, xs[n])] if n is not None else []
            findings.append(_finding("info", title, detail, next_step, where, wins))
            continue
        end = xs[n] if n is not None else xr
        wins = [(xs[p], end)]
        # A reboot empties memory and leaves a gap in the data; restarting the
        # sysstat service writes the same record without either. Caches refill
        # fast, so with 10-minute samples only a halving can be relied on.
        before_after = {
            c: (m[c][p], m[c][n])
            for c in ("kbcached", "kbmemused")
            if c in m and n is not None and m[c][p] and m[c][n] is not None
        }
        emptied = [c for c, (a, b) in before_after.items() if b <= 0.5 * a]
        if before_after and not emptied and end - xs[p] <= 2.5 * iv:
            title = f"sysstat restarted at {_hm(xr)}"
            detail = (
                "Memory stayed in use and no samples are missing, so the host "
                "didn't reboot: the sysstat service was restarted or upgraded."
            )
            next_step = "Nothing to do unless that restart was unexpected."
            findings.append(_finding("info", title, detail, next_step, where, wins))
            continue
        resumes = _hm(end) if n is not None else "never"
        detail = (
            f"Last sample {_hm(xs[p])}, boot record {t}, data resumes {resumes}: "
            f"{(end - xs[p]) / 60:.0f} minutes with no data."
        )
        if emptied:
            a, b = before_after[emptied[0]]
            what = "Page cache" if emptied[0] == "kbcached" else "Memory in use"
            detail += (
                f" {what} fell from {a / GIB:.1f} to {b / GIB:.1f} GiB, "
                "so this was a real reboot."
            )
        elif before_after:
            detail += " The gap in the data points to a reboot, not a sysstat restart."
        else:
            detail += " Without memory data, sar can't confirm it was a reboot."
        title = f"Host rebooted at {_hm(xr)}"
        next_step = "Find the cause: journalctl --list-boots, last -x, and /var/crash."
        findings.append(_finding("warn", title, detail, next_step, where, wins))
    return findings


def _check_steal(report: dict) -> list[dict] | None:
    si, sec = _section(report, "%steal", "CPU")
    if sec is None:
        return None
    xs, cpus = _table(sec, ["%steal", "%idle", "%iowait"])
    total = cpus.get("all")
    steal = total["%steal"] if total else []
    ok = [k for k, v in enumerate(steal) if v is not None]
    if not ok:
        return None
    vcpus = {e: c["%steal"] for e, c in cpus.items() if e != "all"}
    ncpu = report.get("ncpu") or len(vcpus) or 1
    if max(v or 0 for vals in (steal, *vcpus.values()) for v in vals) == 0:
        return []

    idle = [c for c in ("%idle", "%iowait", "%steal") if c in total]

    def work(k: int) -> float:
        """The guest's own CPU use: all but idle, iowait and steal."""
        return 100 - sum(total[c][k] or 0 for c in idle)

    breaks = _restarts(report)
    where = (si, "%steal", ["all"])
    mean = statistics.mean(steal[k] for k in ok)
    med = statistics.median(steal[k] for k in ok)
    k_pk = max(ok, key=lambda k: steal[k])

    high = [v is not None and v >= 10 for v in steal]
    warn = _windows(xs, high, 1200, breaks)
    if warn:
        worse = [
            h and work(k) >= 70 or (steal[k] or 0) >= 20 for k, h in enumerate(high)
        ]
        severity = "crit" if _windows(xs, worse, 1200, breaks) else "warn"
        idx = [k for w in warn for k in w[2]]
        k = max(idx, key=lambda k: steal[k])
        detail = (
            f"The host withheld CPU this VM had work for: steal averaged "
            f"{statistics.mean(steal[j] for j in idx):.1f}% in these windows "
            f"(peak {steal[k]:.1f}% at {_hm(xs[k])}). Causes include an "
            "overloaded host, a noisy neighbour, or exhausted CPU credits on a "
            "burstable cloud instance."
        )
        next_step = (
            "This can't be fixed inside the guest: resize or move the VM, or "
            "raise it with the hypervisor or cloud provider."
        )
        return [_finding(severity, "High CPU steal", detail, next_step, where, warn)]
    for e, vals in vcpus.items():
        hot = _windows(xs, [v is not None and v >= 25 for v in vals], 1200, breaks)
        if hot:
            k = max((k for w in hot for k in w[2]), key=lambda k: vals[k])
            title = f"High CPU steal on vCPU {e}"
            detail = (
                f"vCPU {e} lost up to {vals[k]:.1f}% to steal (peak at "
                f"{_hm(xs[k])}) while the VM as a whole lost {mean:.1f}%, so one "
                "host CPU is likely overloaded or pinned."
            )
            next_step = (
                "Raise it with the hypervisor team: check vCPU pinning on the host."
            )
            where = (si, "%steal", ["all", e])
            return [_finding("warn", title, detail, next_step, where, hot)]

    present = {e: [v for v in vals if v is not None] for e, vals in vcpus.items()}
    present = {e: vals for e, vals in present.items() if vals}
    findings = []
    if med >= 2:
        p95 = sorted(steal[k] for k in ok)[int(0.95 * (len(ok) - 1))]
        detail = (
            f"About {mean * ncpu / 100:.1f} of {ncpu} vCPUs spent waiting on the "
            f"host: mean {mean:.2f}%, p95 {p95:.2f}%, peak {steal[k_pk]:.2f}% at "
            f"{_hm(xs[k_pk])}."
        )
        if present:
            means = {e: statistics.mean(vals) for e, vals in present.items()}
            hot = max(means, key=means.get)
            detail += f" vCPU {hot} was highest at {means[hot]:.1f}% on average."
        peak_work = max(work(k) for k in ok)
        if peak_work < 70:
            detail += (
                f" The guest's own CPU use peaked at {peak_work:.0f}%, so this is "
                "host scheduling delay, not CPU starvation."
            )
        title = f"CPU steal steady at {mean:.1f}%"
        next_step = (
            "It's below the 10% warning level; act only if latency complaints "
            "line up with it."
        )
        findings.append(_finding("info", title, detail, next_step, where))

    # A rise well above this VM's own baseline on nearly every vCPU at once
    # points at the host, e.g. a noisy neighbour.
    usual = {e: statistics.median(vals) for e, vals in present.items()}
    usual_work = statistics.median(work(k) for k in ok)
    threshold = max(2.0, med + max(1.5, 0.4 * med))
    rising = [v is not None and v >= threshold for v in steal]
    for win in _windows(xs, rising, 1200, breaks) if present else []:
        k = max(win[2], key=lambda k: steal[k])
        risen = [e for e, u in usual.items() if (vcpus[e][k] or 0) >= u + 1.0]
        if len(risen) < 0.75 * len(present):
            continue
        rise_work = statistics.mean(work(j) for j in win[2])
        if rise_work - usual_work < 0.5:
            load = f"while the guest's own CPU use stayed flat at {rise_work:.1f}%"
        else:
            load = f"while the guest's CPU use also rose, to {rise_work:.1f}%"
        detail = (
            f"Steal rose to {steal[k]:.2f}% (baseline {med:.2f}%) on {len(risen)} "
            f"of {len(present)} vCPUs at once, {load}. A rise on nearly every vCPU "
            "points to the host side, such as a noisy neighbour."
        )
        next_step = (
            "Compare this window with latency complaints; if they match, raise "
            "it with the hypervisor or cloud team."
        )
        title = "Host-side steal rise"
        findings.append(_finding("info", title, detail, next_step, where, [win]))
    return findings


def _check_cpu(report: dict) -> list[dict] | None:
    si, sec = _section(report, "%idle", "CPU")
    if sec is None:
        return None
    xs, cpus = _table(sec, ["%idle", "%iowait", "%steal", "%usr", "%sys"])
    total = cpus.get("all")
    if not total:
        return None
    # iowait and steal are idle time from the guest's point of view.
    idle = [c for c in ("%idle", "%iowait", "%steal") if c in total]
    busy = [
        None if v is None else 100 - sum(total[c][k] or 0 for c in idle)
        for k, v in enumerate(total["%idle"])
    ]
    breaks = _restarts(report)
    warn = _windows(xs, [b is not None and b >= 90 for b in busy], 600, breaks)
    if not warn:
        return []
    crit = _windows(xs, [b is not None and b >= 98 for b in busy], 600, breaks)
    idx = [k for w in warn for k in w[2]]
    k = max(idx, key=lambda k: busy[k])
    mean = {c: statistics.mean(total[c][j] or 0 for j in idx) for c in total}
    detail = (
        f"CPUs averaged {statistics.mean(busy[j] for j in idx):.0f}% busy in "
        f"these windows (peak {busy[k]:.0f}% at {_hm(xs[k])}): "
        f"{mean.get('%usr', 0):.0f}% user, {mean.get('%sys', 0):.0f}% system."
    )
    severity = "crit" if crit else "warn"
    next_step = "Find the busiest processes in the sosreport's ps or top output."
    where = (si, "%usr", ["all"])
    return [_finding(severity, "CPU saturated", detail, next_step, where, warn)]


def _check_memory(report: dict) -> list[dict] | None:
    si, sec = _section(report, "kbavail")
    if sec is None or not {"kbmemused", "%memused"} <= set(sec["columns"]):
        return None
    xs, table = _table(sec, ["kbavail", "kbmemused", "%memused"])
    m = table.get("")
    pairs = (
        [(u, p) for u, p in zip(m["kbmemused"], m["%memused"]) if u and p] if m else []
    )
    if not pairs:
        return None
    # %memused is kbmemused / MemTotal in every sysstat version, so the ratio
    # of sums recovers MemTotal, which sar doesn't print.
    total = sum(u for u, _ in pairs) / sum(p for _, p in pairs) * 100
    pct = [None if a is None else 100 * a / total for a in m["kbavail"]]
    breaks = _restarts(report)
    warn = _windows(xs, [p is not None and p <= 10 for p in pct], 300, breaks)
    if not warn:
        return []
    crit = _windows(xs, [p is not None and p <= 5 for p in pct], 300, breaks)
    k = min((k for w in warn for k in w[2]), key=lambda k: pct[k])
    detail = (
        f"Available memory fell to {pct[k]:.1f}% of RAM "
        f"({m['kbavail'][k] / GIB:.1f} of {total / GIB:.0f} GiB) at {_hm(xs[k])}."
    )
    _, swap = _section(report, "pswpin/s")
    if swap:
        sxs, st = _table(swap, ["pswpin/s"])
        rates = st.get("", {}).get("pswpin/s", [])
        swapped = [
            v for x, v in zip(sxs, rates) if v and any(a <= x <= b for a, b, _ in warn)
        ]
        if swapped:
            detail += f" Pages were swapped back in at up to {max(swapped):.0f}/s."
    severity = "crit" if crit else "warn"
    next_step = (
        "Check dmesg or the journal for OOM-killer messages, and ps for the "
        "largest processes."
    )
    where = (si, "kbavail", [])
    return [_finding(severity, "Low available memory", detail, next_step, where, warn)]


def _disk_label(name: str) -> str:
    """dev8-16 -> 'dev8-16 (sdb)': major 8 is always SCSI disks, 16 minors each."""
    m = re.fullmatch(r"dev8-(\d+)", name)
    if not m:
        return name
    disk, part = divmod(int(m.group(1)), 16)
    return f"{name} (sd{chr(ord('a') + disk)}{part or ''})"


def _check_disks(report: dict) -> list[dict] | None:
    si, sec = _section(report, "await", "DEV")
    if sec is None or "tps" not in sec["columns"]:
        return None
    xs, devs = _table(sec, ["await", "tps", "%util", "rkB/s", "wkB/s"])
    breaks = _restarts(report)
    slow = {}
    for name, d in devs.items():
        if _SKIP_DISK.match(name):
            continue
        # Little's law: requests in flight = completions/s x seconds each.
        queue = [
            t * a / 1000 if a is not None and t is not None and t >= 1 else None
            for a, t in zip(d["await"], d["tps"])
        ]
        flags = [q is not None and a >= 50 for q, a in zip(queue, d["await"])]
        wins = _windows(xs, flags, 300, breaks)
        if wins:
            slow[name] = (queue, wins)
    # sar -p names device-mapper devices dm-N; others keep their own names.
    hardware = [
        w for name, (_, ws) in slow.items() if not name.startswith("dm-") for w in ws
    ]
    findings = []
    for name, (queue, wins) in slow.items():
        d = devs[name]
        aw = d["await"]
        medians = [statistics.median(aw[k] for k in w[2]) for w in wins]
        idx = wins[medians.index(max(medians))][2]
        k = max(idx, key=lambda k: aw[k])
        inflight = statistics.median(queue[j] for j in idx)
        parts = [
            f"await {max(medians):.0f} ms median (peak {aw[k]:.0f} ms at {_hm(xs[k])})",
            f"about {inflight:.1f} requests in flight",
        ]
        if "rkB/s" in d and "wkB/s" in d:
            rate = statistics.median(
                (d["rkB/s"][j] or 0) + (d["wkB/s"][j] or 0) for j in idx
            )
            parts.append(f"{rate / 1024:.1f} MB/s")
        if "%util" in d:
            parts.append(
                f"%util {statistics.median(d['%util'][j] or 0 for j in idx):.0f}%"
            )
        detail = ("Worst window: " if len(wins) > 1 else "") + ", ".join(parts) + "."
        calm = [a for a, q in zip(aw, queue) if q is not None and q < 0.5]
        if calm:
            usual = statistics.median(calm)
            usual_ms = f"{usual:.1f}" if usual < 10 else f"{usual:.0f}"
            detail += f" This disk normally answers in about {usual_ms} ms."
        overlap = any(a < y and x < b for a, b, _ in wins for x, y, _ in hardware)
        layered = name.startswith("dm-") and not overlap
        if layered:
            detail += (
                " The physical disks didn't slow down at the same time, so the "
                "queue built up in the device-mapper layer (such as encryption "
                "or thin provisioning), not in the hardware."
            )
            next_step = (
                "Check what this device is (lsblk and dmsetup table in the "
                "sosreport) and what was writing through it then."
            )
        elif inflight < 1:
            detail += " Requests are slow even without a queue."
            next_step = (
                "Check the device and its path: dmesg for SCSI or I/O errors, "
                "smartctl, multipath -ll."
            )
        else:
            if inflight >= 4:
                detail += (
                    " The latency is queueing behind a burst of I/O, not slow "
                    "individual requests."
                )
            next_step = (
                "Find what was reading or writing this disk then, such as a "
                "backup or a cron job."
            )
        severity = "crit" if max(medians) >= 200 and not layered else "warn"
        title = f"High disk latency on {_disk_label(name)}"
        where = (si, "await", [name])
        findings.append(_finding(severity, title, detail, next_step, where, wins))
    return findings


def _drop_ratio(drops: list, rx: list, idx: list[int]) -> float:
    return sum(drops[k] for k in idx) / sum(rx[k] + drops[k] for k in idx)


def _check_nic_drops(report: dict) -> list[dict] | None:
    se, err = _section(report, "rxdrop/s", "IFACE")
    _, dev = _section(report, "rxpck/s", "IFACE")
    if err is None or dev is None:
        return None
    xs, drops = _table(err, ["rxdrop/s"])
    pxs, pkts = _table(dev, ["rxpck/s", "%ifutil"])
    at = {x: k for k, x in enumerate(pxs)}
    _, softnet = _section(report, "dropd/s", "CPU")
    backlog = None
    if softnet:
        bxs, bt = _table(softnet, ["dropd/s"])
        backlog = dict(zip(bxs, bt["all"]["dropd/s"])) if "all" in bt else None
    iv = _interval(xs) or 0
    breaks = _restarts(report)
    found = {}
    for name, d in drops.items():
        if _VIRTUAL_IFACE.match(name) or name not in pkts:
            continue
        dr, p = d["rxdrop/s"], pkts[name]
        rx = [p["rxpck/s"][at[x]] if x in at else None for x in xs]
        util = [p["%ifutil"][at[x]] if x in at and "%ifutil" in p else None for x in xs]
        # sar rates an interface missing from the previous record against
        # zero, so that sample is its since-boot totals, not traffic.
        ok = [
            dr[k] is not None
            and rx[k] is not None
            and (k == 0 or dr[k - 1] is not None)
            and (util[k] or 0) <= 100
            and dr[k] < 1e9
            for k in range(len(xs))
        ]
        flags = [
            ok[k] and dr[k] >= 1 and dr[k] >= 0.001 * (rx[k] + dr[k])
            for k in range(len(xs))
        ]
        wins = _windows(xs, flags, 300, breaks)
        if not wins:
            continue
        idx = [k for w in wins for k in w[2]]
        worst = max(_drop_ratio(dr, rx, w[2]) for w in wins)
        quiet = [rx[k] for k in range(len(xs)) if ok[k] and not flags[k]]
        rx_drop = statistics.median(rx[k] for k in idx)
        rx_quiet = statistics.median(quiet) if quiet else 0
        tracks_load = rx_drop >= 2 * rx_quiet or worst >= 0.01
        lost = sum(dr[k] for k in idx) * iv
        k = max(idx, key=lambda k: dr[k])
        detail = (
            f"{_count(lost)} packets dropped, {100 * _drop_ratio(dr, rx, idx):.2f}% "
            f"of inbound traffic in these windows, peak {dr[k]:.0f}/s at {_hm(xs[k])}"
        )
        if tracks_load:
            detail += (
                f", only while inbound traffic was high (about {rx_drop:,.0f} vs "
                f"{rx_quiet:,.0f} packets/s otherwise)."
            )
            next_step = (
                f"Compare ethtool -S {name} counters across a window (missed, "
                f"no_buffer, discard) and check the RX ring size with "
                f"ethtool -g {name}."
            )
        else:
            detail += (
                ", at normal traffic levels, which suggests frames the kernel "
                "discards on purpose, such as an unhandled protocol or VLAN."
            )
            next_step = (
                f"Check ip -s -s link show {name}: if 'missed' stays flat while "
                "'dropped' grows, these are discarded frames, not lost traffic."
            )
        if backlog is not None and any(backlog.get(xs[j]) for j in idx):
            detail += (
                " The kernel backlog dropped packets too, so the CPUs weren't "
                "keeping up."
            )
        elif backlog is not None:
            detail += (
                " Kernel backlog drops were zero, so packets were lost at the NIC or "
                "driver; sar can't tell a full RX ring from a hardware discard."
            )
        severity = "crit" if worst >= 0.01 else "warn" if tracks_load else "info"
        title = f"Inbound packet drops on {name}"
        where = (se, "rxdrop/s", [name])
        found[name] = (lost, _finding(severity, title, detail, next_step, where, wins))

    # A bond's counters are the sum of its members', so a bond whose drops
    # match a member's is the same loss counted twice.
    members = {o: n for o, (n, _) in found.items() if not _BOND.match(o)}
    findings = []
    for name, (lost, finding) in found.items():
        twin = [o for o, n in members.items() if abs(n - lost) <= 0.05 * lost]
        if _BOND.match(name) and twin:
            found[twin[0]][1]["detail"] += f" {name} reports the same drops."
        else:
            findings.append(finding)
    return findings


_CHECKS = (
    ("reboot", _check_reboots),
    ("CPU steal", _check_steal),
    ("CPU saturation", _check_cpu),
    ("available memory", _check_memory),
    ("disk latency", _check_disks),
    ("network drops", _check_nic_drops),
)


def analyze(report: dict) -> dict:
    """Run every check on a report dict; findings come back most severe first."""
    findings: list[dict] = []
    context: list[str] = []
    missing = []
    for label, check in _CHECKS:
        try:
            found = check(report)
        # One broken check mustn't hide the others' findings.
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            context.append(f"The {label} check failed here ({type(exc).__name__}).")
            continue
        if found is None:
            missing.append(label)
        else:
            findings.extend(found)
    if missing:
        context.append(
            f"Not checked, because the data isn't in this file: {', '.join(missing)}."
        )
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["windows"][:1]))
    return {"findings": findings, "context": context}

/* sargeant front-end controller.
 *
 * Talks to the stdlib backend (/api/files, /api/data), renders sar sections as
 * fast uPlot time-series charts. Stays data-driven: section/column labels come
 * straight from the parsed report, so new sar sections render without code
 * changes (a small FRIENDLY map only prettifies names we happen to recognize).
 */
"use strict";

const state = {
    report: null, // current parsed report
    section: null, // selected Section object
    entities: new Set(), // selected entity keys (for CPU/DEV/IFACE sections)
};

// Optional cosmetic labels. Anything not here falls back to the raw column —
// this is presentation only, never required for a section to render.
const FRIENDLY = {
    "%usr": "CPU utilisation",
    "kbmemfree": "Memory",
    "kbswpfree": "Swap usage",
    "kbhugfree": "Huge pages",
    "proc/s": "Task creation / context switches",
    "pgpgin/s": "Paging",
    "pswpin/s": "Swapping",
    "tps": "I/O transfer rate",
    "runq-sz": "Load & run queue",
    "dentunusd": "Inode & file handles",
    "rxpck/s": "Network throughput",
    "rxerr/s": "Network errors",
    "totsck": "Sockets",
    "call/s": "NFS client",
    "scall/s": "NFS server",
    "rcvin/s": "Serial (TTY)",
    "total/s": "Softnet",
    "%scpu-10": "CPU pressure (PSI)",
    "%sio-10": "I/O pressure (PSI)",
    "%smem-10": "Memory pressure (PSI)",
};

const PALETTE = ["#06c", "#e95420", "#0e8420", "#c7162b", "#f99b11", "#7a2dc0",
    "#00a3a3", "#b34b00", "#5c5c5c", "#2a7de1"
];

// utils
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
};

function hmsToSeconds(t) {
    const [h, m, s] = t.split(":").map(Number);
    return h * 3600 + m * 60 + s;
}

// Seconds-since-midnight (possibly past 86400 after a rollover) -> "HH:MM".
function fmtHM(secs) {
    const h = Math.floor(secs / 3600),
        m = Math.floor((secs % 3600) / 60);
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// Seconds-since-day0-midnight (can exceed 86400 when sar rolls past midnight)
// -> "YYYY-MM-DD HH:MM" given the report's start date. Without a date, "HH:MM".
function fmtDateTime(day0, secs) {
    const time = fmtHM(((secs % 86400) + 86400) % 86400);
    if (!day0) return time;
    const dayIdx = Math.floor(secs / 86400);
    if (dayIdx === 0) return `${day0} ${time}`;
    const d = new Date(`${day0}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + dayIdx);
    return `${d.toISOString().slice(0, 10)} ${time}`;
}

// "YYYY-MM-DD HH:MM → HH:MM" when the window stays on one date; shows both dates
// when it crosses midnight; degrades to "HH:MM → HH:MM" when no date is known.
function windowLabel(day0, min, max) {
    const s = fmtDateTime(day0, min),
        e = fmtDateTime(day0, max);
    const sp = s.split(" "),
        ep = e.split(" ");
    if (sp.length === 2 && ep.length === 2 && sp[0] === ep[0]) {
        return `${sp[0]} ${sp[1]} → ${ep[1]}`;
    }
    return `${s} → ${e}`;
}

function sectionLabel(sec) {
    const first = sec.columns[0];
    const friendly = FRIENDLY[first];
    if (friendly) return sec.key ? `${friendly} · per ${sec.key}` : friendly;
    return sec.name;
}

// Default entity selection: keep charts legible out of the box.
function defaultEntities(sec) {
    if (!sec.key) return [];
    if (sec.entities.includes("all")) return ["all"]; // CPU
    return sec.entities.slice(0, 4); // first few DEV/IFACE
}

// data
async function loadFiles(selectName) {
    const r = await fetch("/api/files");
    const {
        files,
        uploads
    } = await r.json();
    $("#upload-block").hidden = !uploads; // only shown in hosted mode
    const sel = $("#file-select");
    sel.innerHTML = "";
    files.forEach((f) => {
        const o = el("option", null, `${f.day || f.name} (${f.name})`);
        o.value = f.name;
        sel.appendChild(o);
    });
    if (files.length) {
        const hash = location.hash.match(/^#([^/]+)\//);
        const want = (selectName && files.some((f) => f.name === selectName)) ? selectName :
            (hash && files.some((f) => f.name === hash[1])) ? hash[1] :
            files[files.length - 1].name; // chosen, else deep-linked, else newest
        sel.value = want;
        await loadReport(sel.value);
    } else {
        // Fresh session (hosted) or empty dir: nothing to plot yet.
        $("#charts").innerHTML = "";
        $("#section-list").innerHTML = "";
        $("#entity-bar").classList.add("u-hide");
        $("#empty").textContent = uploads ?
            "Upload a sar text file on the left to analyse it." :
            "No sar files found.";
        $("#empty").classList.remove("u-hide");
    }
}

// ---------------------------------------------------------------- uploads
async function uploadFiles(fileList) {
    const status = $("#upload-status");
    const btn = $("#upload-btn");
    const files = [...fileList];
    if (!files.length) {
        status.textContent = "Choose a file first.";
        return;
    }
    btn.disabled = true;
    let lastOk = null;
    let loaded = 0;
    const skipped = [];
    for (let i = 0; i < files.length; i++) {
        const f = files[i];
        status.textContent = `Uploading ${i + 1}/${files.length}: ${f.name}…`;
        try {
            const r = await fetch(`/api/upload?name=${encodeURIComponent(f.name)}`, {
                method: "POST",
                body: f,
            });
            const j = await r.json().catch(() => ({}));
            if (r.ok) {
                loaded++;
                lastOk = j.name;
            } else {
                // Skip what the server can't use (binary saNN, over quota, …) and
                // keep going, rather than aborting the whole batch.
                skipped.push(`${f.name}: ${j.error || r.status}`);
            }
        } catch (err) {
            skipped.push(`${f.name}: ${err}`);
        }
    }
    btn.disabled = false;
    $("#upload-input").value = "";

    let msg = loaded ? `Loaded ${loaded} file${loaded === 1 ? "" : "s"}.` : "No files loaded.";
    if (skipped.length) {
        const eg = skipped.length > 1 ? "e.g. " : "";
        msg += ` Skipped ${skipped.length}, ${eg}${skipped[0]}.`;
    }
    status.textContent = msg;
    if (lastOk) await loadFiles(lastOk);
}

async function loadReport(name) {
    const r = await fetch(`/api/data?file=${encodeURIComponent(name)}`);
    if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        alert(`Failed to load ${name}: ${e.error || r.status}`);
        return;
    }
    // Remember the metric we're viewing so switching files keeps us on it.
    const prev = state.section;
    state.report = await r.json();
    state.section = null;
    $("#host-meta").textContent =
        `${state.report.host || ""} · ${state.report.kernel || ""} · ${state.report.day || ""}`;
    renderSectionList();
    const items = document.querySelectorAll(".p-side-nav__item");
    const sections = state.report.sections;
    // Priority: a deep link for this file, else the same metric we were on (so
    // changing the day doesn't reset us to CPU), else the first metric.
    const hash = location.hash.match(/^#([^/]+)\/(\d+)$/);
    let wantIdx = 0;
    if (hash && hash[1] === name) {
        wantIdx = Math.min(+hash[2], items.length - 1);
    } else if (prev) {
        const byName = sections.findIndex((s) => s.name === prev.name);
        if (byName >= 0) wantIdx = byName;
    }
    if (items.length) {
        selectSection(wantIdx, items[wantIdx]);
    } else {
        $("#charts").innerHTML = "";
        $("#entity-bar").classList.add("u-hide");
        $("#empty").classList.remove("u-hide");
    }
}

// nav
function renderSectionList() {
    const ul = $("#section-list");
    ul.innerHTML = "";
    state.report.sections.forEach((sec, i) => {
        const li = el("li", "p-side-nav__item");
        li.appendChild(el("span", null, sectionLabel(sec)));
        const meta = sec.key ?
            `${sec.entities.length}×${sec.columns.length}` :
            `${sec.columns.length}`;
        li.appendChild(el("span", "p-side-nav__count", meta));
        li.addEventListener("click", () => selectSection(i, li));
        ul.appendChild(li);
    });
}

function selectSection(i, li) {
    document.querySelectorAll(".p-side-nav__item")
        .forEach((n) => n.classList.remove("is-active"));
    li.classList.add("is-active");
    state.section = state.report.sections[i];
    state.entities = new Set(defaultEntities(state.section));
    if ($("#file-select").value) {
        history.replaceState(null, "", `#${$("#file-select").value}/${i}`);
    }
    renderEntityBar();
    renderCharts();
}

function renderEntityBar() {
    const bar = $("#entity-bar");
    const chips = $("#entity-chips");
    chips.innerHTML = "";
    if (!state.section.key) {
        bar.classList.add("u-hide");
        return;
    }
    bar.classList.remove("u-hide");
    state.section.entities.forEach((e) => {
        const chip = el("button", "p-chip", e);
        if (state.entities.has(e)) chip.classList.add("is-on");
        chip.addEventListener("click", () => {
            if (state.entities.has(e)) state.entities.delete(e);
            else state.entities.add(e);
            chip.classList.toggle("is-on");
            renderCharts();
        });
        chips.appendChild(chip);
    });
}

// charts
function buildSeriesData(sec, colIdx) {
    // Returns [xs, ...ySeries] aligned on the union of timestamps.
    // For keyed sections one y-series per selected entity; else a single series.
    const wantEntities = sec.key ? [...state.entities] : [""];

    // Collect ordered unique timestamps. sar's final reading of a day wraps to
    // 00:00:00; carry a day-offset so x stays monotonic (required by uPlot).
    const tIndex = new Map();
    const xs = [];
    let dayOffset = 0;
    let prev = -1;
    for (const row of sec.rows) {
        if (tIndex.has(row.t)) continue;
        let secs = hmsToSeconds(row.t);
        if (secs < prev) dayOffset += 86400; // crossed midnight
        prev = secs;
        secs += dayOffset;
        tIndex.set(row.t, xs.length);
        xs.push(secs);
    }
    const series = wantEntities.map(() => new Array(xs.length).fill(null));
    const entPos = new Map(wantEntities.map((e, i) => [e, i]));

    for (const row of sec.rows) {
        const ent = sec.key ? row.e : "";
        if (!entPos.has(ent)) continue;
        series[entPos.get(ent)][tIndex.get(row.t)] = row.v[colIdx];
    }
    return {
        xs,
        series,
        labels: wantEntities
    };
}

function makeChart(host, sec, colIdx) {
    const colName = sec.columns[colIdx];
    const {
        xs,
        series,
        labels
    } = buildSeriesData(sec, colIdx);

    const card = el("div", "p-card");
    const head = el("div", "p-card__header");
    head.appendChild(el("span", "p-card__title", colName));
    head.appendChild(el("span", "p-card__sub", sec.key ? `per ${sec.key}` : "system-wide"));
    card.appendChild(head);

    // Read-only bar above the chart: a proportional segment showing the visible
    // time window relative to the whole day, plus the window as text. It mirrors
    // the drag-zoom on the chart (and the double-click reset).
    const tlRow = el("div", "chart-timeline-row");
    const track = el("div", "chart-timeline");
    const fill = el("div", "chart-timeline__fill");
    track.appendChild(fill);
    const tlLabel = el("span", "chart-timeline__label");
    tlRow.appendChild(track);
    tlRow.appendChild(tlLabel);
    card.appendChild(tlRow);

    const mount = el("div", "chart-host");
    card.appendChild(mount);
    host.appendChild(card);

    const full0 = xs.length ? xs[0] : 0;
    const full1 = xs.length ? xs[xs.length - 1] : 0;
    const fullSpan = full1 - full0 || 1;
    const day0 = state.report && state.report.day ? state.report.day : null;

    function updateTimeline(u) {
        let min = u.scales.x.min,
            max = u.scales.x.max;
        if (min == null || max == null) {
            min = full0;
            max = full1;
        }
        // Clamp to the data range (uPlot can pad the scale a little).
        min = Math.max(min, full0);
        max = Math.min(max, full1);
        const left = ((min - full0) / fullSpan) * 100;
        const width = ((max - min) / fullSpan) * 100;
        fill.style.left = `${Math.max(0, Math.min(100, left))}%`;
        fill.style.width = `${Math.max(0, Math.min(100, width))}%`;
        tlLabel.textContent = windowLabel(day0, min, max);
    }

    const uSeries = [{}];
    series.forEach((_, i) => {
        uSeries.push({
            label: labels[i] || colName,
            stroke: PALETTE[i % PALETTE.length],
            width: 1.5,
            points: {
                show: false
            },
            spanGaps: true,
        });
    });

    const opts = {
        width: mount.clientWidth || 800,
        height: 220,
        cursor: {
            drag: {
                x: true,
                y: false
            }
        },
        scales: {
            x: {
                time: false
            }
        },
        axes: [{
                values: (u, vals) => vals.map(fmtHM),
            },
            {},
        ],
        series: uSeries,
        hooks: {
            setScale: [(u, key) => {
                if (key !== "x") return;
                updateTimeline(u);
                syncZoom(u); // mirror this window onto every other chart
            }],
        },
    };

    const u = new uPlot(opts, [xs, ...series], mount);
    updateTimeline(u); // initial full-range state
    return u;
}

let resizeObs = null;
let chartInstances = []; // the current section's uPlot charts (for zoom sync)
let zoomSyncing = false; // re-entrancy guard while mirroring a zoom

// Mirror one chart's x-window onto every other chart on the page, so zooming
// (or double-click resetting) any chart correlates the same window across all.
function syncZoom(src) {
    if (zoomSyncing) return;
    zoomSyncing = true;
    const {
        min,
        max
    } = src.scales.x;
    for (const c of chartInstances) {
        if (c !== src && (c.scales.x.min !== min || c.scales.x.max !== max)) {
            c.setScale("x", {
                min,
                max
            });
        }
    }
    zoomSyncing = false;
}

function renderCharts() {
    const host = $("#charts");
    chartInstances.forEach((c) => c.destroy());
    chartInstances = [];
    host.innerHTML = "";
    $("#empty").classList.add("u-hide");
    const sec = state.section;
    if (!sec) return;

    sec.columns.forEach((_, idx) => chartInstances.push(makeChart(host, sec, idx)));

    // Keep charts sized to their container.
    if (resizeObs) resizeObs.disconnect();
    resizeObs = new ResizeObserver(() => {
        chartInstances.forEach((c) => {
            const w = c.root.parentElement.clientWidth;
            if (w && Math.abs(w - c.width) > 2) c.setSize({
                width: w,
                height: 220
            });
        });
    });
    resizeObs.observe(host);
}

// init
$("#file-select").addEventListener("change", (e) => loadReport(e.target.value));
$("#upload-btn").addEventListener("click", () => uploadFiles($("#upload-input").files));
loadFiles().catch((err) => {
    document.body.insertBefore(
        el("div", "p-empty", `Could not reach backend: ${err}`), document.body.firstChild);
});

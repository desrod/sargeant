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

// ------------------------------------------------------------------ utils
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

// ------------------------------------------------------------------ data
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
    for (const f of files) {
        status.textContent = `Uploading ${f.name}…`;
        try {
            const r = await fetch(`/api/upload?name=${encodeURIComponent(f.name)}`, {
                method: "POST",
                body: f,
            });
            const j = await r.json().catch(() => ({}));
            if (!r.ok) {
                status.textContent = `${f.name}: ${j.error || r.status}`;
                break;
            }
            lastOk = j.name;
        } catch (err) {
            status.textContent = `${f.name}: ${err}`;
            break;
        }
    }
    btn.disabled = false;
    if (lastOk) {
        status.textContent = `Loaded ${lastOk}.`;
        $("#upload-input").value = "";
        await loadFiles(lastOk);
    }
}

async function loadReport(name) {
    const r = await fetch(`/api/data?file=${encodeURIComponent(name)}`);
    if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        alert(`Failed to load ${name}: ${e.error || r.status}`);
        return;
    }
    state.report = await r.json();
    state.section = null;
    $("#host-meta").textContent =
        `${state.report.host || ""} · ${state.report.kernel || ""} · ${state.report.day || ""}`;
    renderSectionList();
    // Honour a deep link (#file/sectionIndex); else show the first metric so the
    // user never lands on a dead empty state.
    const items = document.querySelectorAll(".p-side-nav__item");
    const hash = location.hash.match(/^#([^/]+)\/(\d+)$/);
    const wantIdx = hash && hash[1] === name ? Math.min(+hash[2], items.length - 1) : 0;
    if (items.length) {
        selectSection(wantIdx, items[wantIdx]);
    } else {
        $("#charts").innerHTML = "";
        $("#entity-bar").classList.add("u-hide");
        $("#empty").classList.remove("u-hide");
    }
}

// ------------------------------------------------------------------ nav
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

// ------------------------------------------------------------------ charts
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
    const mount = el("div", "chart-host");
    card.appendChild(mount);
    host.appendChild(card);

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
                values: (u, vals) => vals.map((v) => {
                    const h = Math.floor(v / 3600),
                        m = Math.floor((v % 3600) / 60);
                    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
                }),
            },
            {},
        ],
        series: uSeries,
    };

    return new uPlot(opts, [xs, ...series], mount);
}

let resizeObs = null;

function renderCharts() {
    const host = $("#charts");
    host.innerHTML = "";
    $("#empty").classList.add("u-hide");
    const sec = state.section;
    if (!sec) return;

    const charts = [];
    sec.columns.forEach((_, idx) => charts.push(makeChart(host, sec, idx)));

    // Keep charts sized to their container.
    if (resizeObs) resizeObs.disconnect();
    resizeObs = new ResizeObserver(() => {
        charts.forEach((c) => {
            const w = c.root.parentElement.clientWidth;
            if (w && Math.abs(w - c.width) > 2) c.setSize({
                width: w,
                height: 220
            });
        });
    });
    resizeObs.observe(host);
}

// ------------------------------------------------------------------ init
$("#file-select").addEventListener("change", (e) => loadReport(e.target.value));
$("#upload-btn").addEventListener("click", () => uploadFiles($("#upload-input").files));
loadFiles().catch((err) => {
    document.body.insertBefore(
        el("div", "p-empty", `Could not reach backend: ${err}`), document.body.firstChild);
});

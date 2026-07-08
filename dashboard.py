"""
Tender dashboard — a small local Flask app over the scraped data.

Tabs:
  a. GeM / CPPP tenders      (source in {gem, cppp})
  b. State-wise e-procurement (source is a state name, e.g. telangana/andhra_pradesh)
  c. Empanelments            (any source; title/desc matches empanelment/EOI/
                              rate-contract/enlistment patterns — a cross-cut view)

Refresh button:
  POST /api/refresh triggers an INCREMENTAL re-scrape (run_scraper.py --source all).
  A 5-minute cooldown is enforced: refreshing too soon returns how long to wait.
  Because scraping is incremental (already-seen tenders are skipped), a refresh is
  fast — it only pulls what's new.

Run:
    pip install -r requirements.txt        # flask is included
    python dashboard.py
    # open http://127.0.0.1:5000
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
import time

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "outputs")
CSV_PATH = os.path.join(OUT_DIR, "tenders_gem_cppp.csv")
REFRESH_LOG = os.path.join(OUT_DIR, "refresh.log")
LAST_REFRESH_FILE = os.path.join(OUT_DIR, ".last_refresh")

COOLDOWN_SECONDS = 300           # "wait at least 5 minutes"
EST_REFRESH_SECONDS = 300        # rough upper bound shown to the user for an incremental refresh

from keywords import is_empanelment as _text_is_empanelment  # shared pattern

# Source categorization for the tabs. Central = GeM/CPPP; PSU = dedicated PSU
# portals; everything else is a state e-procurement portal.
CENTRAL_SOURCES = {"gem", "cppp"}
PSU_SOURCES = {"gail", "coalindia", "ntpc", "bhel"}

app = Flask(__name__)

# Portal listing pages, used as a fallback link when a tender has no direct,
# shareable detail URL (its detail is a JS postback or WAF/session-guarded, like
# CPPP). Clicking takes you to the source's live-tender list to search the ref.
SOURCE_LISTING_URLS = {
    "cppp": "https://eprocure.gov.in/cppp/latestactivetendersnew",
    "haryana": "https://works.haryana.gov.in/LiveTender.aspx",
    "telangana": "https://tender.telangana.gov.in/",
    "andhra_pradesh": "https://tender.apeprocurement.gov.in/",
    "gail": "https://gailtenders.in/Gailtenders/Home.asp",
    "bpcl": "https://bpcltenders.eproc.in/Product/publicDash",
}

# Background refresh process handle (single-flight).
_refresh_proc: subprocess.Popen | None = None


def with_link(row: dict) -> dict:
    """Attach a usable link + its type: 'direct' (deep link) or 'portal' (list)."""
    r = dict(row)
    if r.get("source_url"):
        r["link_type"] = "direct"
    else:
        r["source_url"] = SOURCE_LISTING_URLS.get((r.get("source") or "").lower(), "")
        r["link_type"] = "portal" if r["source_url"] else ""
    return r


# --------------------------------------------------------------------------- data
def load_rows() -> list[dict]:
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def tab_of(row: dict) -> str:
    src = (row.get("source") or "").lower()
    if src in CENTRAL_SOURCES:
        return "gem_cppp"
    if src in PSU_SOURCES:
        return "psu"
    return "state"


def is_empanelment(row: dict) -> bool:
    # Prefer the flag written at scrape time; fall back to text match.
    if row.get("is_empanelment"):
        return True
    return _text_is_empanelment(row.get("title", ""), row.get("description", ""))


def has_keyword(row: dict) -> bool:
    return bool((row.get("matched_keywords") or "").strip())


# --------------------------------------------------------------------------- api
@app.route("/api/tenders")
def api_tenders():
    tab = request.args.get("tab", "gem_cppp")
    q = (request.args.get("q") or "").strip().lower()
    page = max(int(request.args.get("page", 1)), 1)
    per = min(max(int(request.args.get("per", 50)), 1), 500)

    rows = load_rows()
    if tab == "empanelment":
        rows = [r for r in rows if is_empanelment(r)]
    else:
        # Tender tabs are IT-only: a row kept solely because it's an
        # empanelment (no keyword) must not appear here.
        rows = [r for r in rows if tab_of(r) == tab and has_keyword(r)]

    if q:
        rows = [r for r in rows
                if q in f"{r.get('title','')} {r.get('organization','')} "
                        f"{r.get('tender_id','')} {r.get('matched_keywords','')} "
                        f"{r.get('source','')}".lower()]

    total = len(rows)
    start = (page - 1) * per
    return jsonify({
        "total": total,
        "page": page,
        "per": per,
        "rows": [with_link(r) for r in rows[start:start + per]],
    })


@app.route("/api/counts")
def api_counts():
    rows = load_rows()
    return jsonify({
        "gem_cppp": sum(1 for r in rows if tab_of(r) == "gem_cppp" and has_keyword(r)),
        "state": sum(1 for r in rows if tab_of(r) == "state" and has_keyword(r)),
        "psu": sum(1 for r in rows if tab_of(r) == "psu" and has_keyword(r)),
        "empanelment": sum(1 for r in rows if is_empanelment(r)),
        "total": len(rows),
        "last_refresh": _last_refresh_iso(),
    })


def _last_refresh_ts() -> float:
    try:
        with open(LAST_REFRESH_FILE) as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return 0.0


def _last_refresh_iso() -> str:
    ts = _last_refresh_ts()
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    global _refresh_proc

    # Already running?
    if _refresh_proc is not None and _refresh_proc.poll() is None:
        return jsonify({"status": "running",
                        "message": "A refresh is already in progress."})

    # Cooldown check.
    elapsed = time.time() - _last_refresh_ts()
    if elapsed < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - elapsed)
        return jsonify({
            "status": "cooldown",
            "wait_seconds": wait,
            "message": f"Refreshed too recently. Please wait at least "
                       f"{wait // 60}m {wait % 60}s before scraping again.",
        }), 429

    # Launch an incremental scrape in the background.
    logf = open(REFRESH_LOG, "w", encoding="utf-8")
    _refresh_proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "run_scraper.py"), "--source", "all"],
        cwd=HERE, stdout=logf, stderr=subprocess.STDOUT,
    )
    with open(LAST_REFRESH_FILE, "w") as fh:
        fh.write(str(time.time()))
    return jsonify({
        "status": "started",
        "estimated_seconds": EST_REFRESH_SECONDS,
        "message": f"Refresh started. Incremental scrape — estimated up to "
                   f"~{EST_REFRESH_SECONDS // 60} minutes (usually much less; "
                   f"only new tenders are fetched).",
    })


@app.route("/api/refresh/status")
def api_refresh_status():
    global _refresh_proc
    running = _refresh_proc is not None and _refresh_proc.poll() is None
    last_line = ""
    if os.path.exists(REFRESH_LOG):
        try:
            with open(REFRESH_LOG, encoding="utf-8", errors="replace") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            last_line = lines[-1] if lines else ""
        except OSError:
            pass
    return jsonify({"running": running, "last_line": last_line,
                    "last_refresh": _last_refresh_iso()})


# --------------------------------------------------------------------------- ui
@app.route("/")
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tender Dashboard</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --accent:#38bdf8; --text:#e2e8f0; --muted:#94a3b8; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); }
  header { padding:16px 24px; background:var(--card); display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; }
  .tabs { display:flex; gap:4px; margin-left:8px; }
  .tab { padding:8px 14px; border:none; background:transparent; color:var(--muted); cursor:pointer; border-radius:6px; font-size:14px; }
  .tab.active { background:var(--accent); color:#04263a; font-weight:600; }
  .tab .badge { background:rgba(255,255,255,.15); padding:1px 7px; border-radius:10px; margin-left:6px; font-size:12px; }
  .spacer { flex:1; }
  input[type=search] { padding:8px 12px; border-radius:6px; border:1px solid #334155; background:#0b1220; color:var(--text); width:260px; }
  button.refresh { padding:8px 16px; background:var(--accent); color:#04263a; border:none; border-radius:6px; font-weight:600; cursor:pointer; }
  button.refresh:disabled { opacity:.5; cursor:not-allowed; }
  #status { padding:8px 24px; font-size:13px; color:var(--muted); min-height:20px; }
  #status.warn { color:#fbbf24; } #status.ok { color:#4ade80; }
  main { padding:0 24px 40px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid #334155; vertical-align:top; }
  th { color:var(--muted); position:sticky; top:0; background:var(--bg); cursor:pointer; }
  tr:hover td { background:#172033; }
  a { color:var(--accent); }
  .kw { color:#a5b4fc; font-size:12px; }
  .pager { margin:16px 0; display:flex; gap:8px; align-items:center; }
  .pager button { padding:6px 12px; background:var(--card); color:var(--text); border:1px solid #334155; border-radius:6px; cursor:pointer; }
  .empty { color:var(--muted); padding:40px; text-align:center; }
</style></head>
<body>
<header>
  <h1>🏛️ Tender Dashboard</h1>
  <div class="tabs" id="tabs">
    <button class="tab active" data-tab="gem_cppp">GeM / CPPP <span class="badge" id="b-gem_cppp">–</span></button>
    <button class="tab" data-tab="state">State-wise <span class="badge" id="b-state">–</span></button>
    <button class="tab" data-tab="psu">PSUs <span class="badge" id="b-psu">–</span></button>
    <button class="tab" data-tab="empanelment">Empanelments <span class="badge" id="b-empanelment">–</span></button>
  </div>
  <div class="spacer"></div>
  <input type="search" id="q" placeholder="Search title / org / id / keyword…">
  <button class="refresh" id="refreshBtn">↻ Refresh</button>
</header>
<div id="status"></div>
<main>
  <table>
    <thead><tr>
      <th>Closing date</th><th>Title</th><th>Organization</th>
      <th>Source</th><th>Tender ID</th><th>Keywords</th><th>Link</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="emptyMsg" class="empty" style="display:none"></div>
  <div class="pager">
    <button id="prev">‹ Prev</button>
    <span id="pageinfo"></span>
    <button id="next">Next ›</button>
  </div>
</main>
<script>
let tab="gem_cppp", page=1, q="", statusTimer=null;
const $=s=>document.querySelector(s);

async function loadCounts(){
  const c=await (await fetch("/api/counts")).json();
  for(const k of ["gem_cppp","state","psu","empanelment"]) $("#b-"+k).textContent=c[k];
  if(c.last_refresh) setStatus("Last updated: "+c.last_refresh,"");
}
async function load(){
  const r=await (await fetch(`/api/tenders?tab=${tab}&q=${encodeURIComponent(q)}&page=${page}&per=50`)).json();
  const tb=$("#rows"); tb.innerHTML="";
  if(!r.rows.length){
    $("#emptyMsg").style.display="block";
    $("#emptyMsg").textContent = tab==="state"
      ? "No state-wise tenders yet — Telangana/Andhra Pradesh sources are pending."
      : "No results.";
  } else $("#emptyMsg").style.display="none";
  for(const t of r.rows){
    const tr=document.createElement("tr");
    const link = t.source_url
      ? `<a href="${t.source_url}" target="_blank" title="${t.link_type==='direct'?'Direct link to this tender':'Opens the source portal listing — search the ref no.'}">${t.link_type==='direct'?'open':'portal ↗'}</a>`
      : "—";
    tr.innerHTML=`<td>${esc(t.closing_date)}</td><td>${esc(t.title)}</td>
      <td>${esc(t.organization)}</td><td>${esc(t.source)}</td>
      <td>${esc(t.tender_id)}</td><td class="kw">${esc(t.matched_keywords)}</td><td>${link}</td>`;
    tb.appendChild(tr);
  }
  const pages=Math.max(Math.ceil(r.total/r.per),1);
  $("#pageinfo").textContent=`Page ${r.page} / ${pages} · ${r.total} rows`;
  $("#prev").disabled=r.page<=1; $("#next").disabled=r.page>=pages;
}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function setStatus(msg,cls){const s=$("#status");s.textContent=msg;s.className=cls||"";}

$("#tabs").onclick=e=>{const b=e.target.closest(".tab"); if(!b)return;
  document.querySelectorAll(".tab").forEach(t=>t.classList.remove("active"));
  b.classList.add("active"); tab=b.dataset.tab; page=1; load();};
$("#q").oninput=e=>{q=e.target.value; page=1; clearTimeout(window._qt); window._qt=setTimeout(load,250);};
$("#prev").onclick=()=>{if(page>1){page--;load();}};
$("#next").onclick=()=>{page++;load();};

$("#refreshBtn").onclick=async()=>{
  const btn=$("#refreshBtn"); btn.disabled=true;
  const r=await (await fetch("/api/refresh",{method:"POST"})).json();
  setStatus(r.message, r.status==="cooldown"?"warn":"ok");
  if(r.status==="started"||r.status==="running"){ pollStatus(); }
  else { btn.disabled=false; }
};
async function pollStatus(){
  clearInterval(statusTimer);
  statusTimer=setInterval(async()=>{
    const s=await (await fetch("/api/refresh/status")).json();
    if(s.running){ setStatus("Scraping… "+(s.last_line||""),"warn"); }
    else{ clearInterval(statusTimer); setStatus("Refresh complete. Last updated: "+s.last_refresh,"ok");
          $("#refreshBtn").disabled=false; loadCounts(); load(); }
  },2000);
}
loadCounts(); load();
</script>
</body></html>"""


if __name__ == "__main__":
    print(f"Tender dashboard -> http://127.0.0.1:5000   (data: {CSV_PATH})")
    app.run(host="127.0.0.1", port=5000, debug=False)

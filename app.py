import os
import json
import subprocess
import threading
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import docker
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# ──────────────────────────────────────────────
# Config — edit these to change behaviour
# ──────────────────────────────────────────────
PLEX_RESTART_DELAY      = int(os.getenv("PLEX_RESTART_DELAY", 60))       # seconds after rclone/zurg restart
WATCHDOG_INTERVAL       = int(os.getenv("WATCHDOG_INTERVAL", 30))        # seconds between watchdog checks
PLEXTRAKTSYNC_SCHEDULE  = os.getenv("PLEXTRAKTSYNC_SCHEDULE", "0 6,18 * * *")  # cron: 06:00 and 18:00
PLEXTRAKTSYNC_CONTAINER = os.getenv("PLEXTRAKTSYNC_CONTAINER", "plextraktsync")
PLEXTRAKTSYNC_COMPOSE   = os.getenv("PLEXTRAKTSYNC_COMPOSE", "/plextraktsync")
PLEX_CONTAINER          = os.getenv("PLEX_CONTAINER", "plex")
WATCH_CONTAINERS        = os.getenv("WATCH_CONTAINERS", "zurg,rclone").split(",")
LOG_MAX_LINES           = 200

# ──────────────────────────────────────────────
# State
# ──────────────────────────────────────────────
state = {
    "events": [],          # list of dicts: {time, type, message}
    "last_sync": None,
    "next_sync": None,
    "sync_status": "idle", # idle | running | success | failed
    "plex_restarts": 0,
    "container_starts": {},  # container_name -> last seen StartedAt
    "sync_stats": {        # stats from last PlexTraktSync run
        "movies_synced": None,
        "shows_synced": None,
        "ratings_synced": None,
        "watched_synced": None,
        "duration": None,
    },
    "sync_history": [],    # list of past sync results (max 20)
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watchdog")

docker_client = docker.from_env()


def add_event(etype: str, message: str):
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": etype,   # info | warn | error | success
        "message": message,
    }
    state["events"].insert(0, entry)
    state["events"] = state["events"][:LOG_MAX_LINES]
    log.info(f"[{etype.upper()}] {message}")


def get_container_started_at(name: str) -> str | None:
    try:
        c = docker_client.containers.get(name)
        return c.attrs["State"]["StartedAt"]
    except Exception:
        return None


def get_container_status(name: str) -> dict:
    try:
        c = docker_client.containers.get(name)
        status = c.attrs["State"]["Status"]
        started = c.attrs["State"]["StartedAt"]
        return {"name": name, "status": status, "started": started, "found": True}
    except Exception:
        return {"name": name, "status": "not found", "started": None, "found": False}


# ──────────────────────────────────────────────
# Watchdog loop
# ──────────────────────────────────────────────
def watchdog_loop():
    add_event("info", "Watchdog gestart")
    while True:
        try:
            for container_name in WATCH_CONTAINERS:
                current_start = get_container_started_at(container_name)
                if current_start is None:
                    continue

                previous_start = state["container_starts"].get(container_name)

                if previous_start is None:
                    # First run — just record
                    state["container_starts"][container_name] = current_start
                    add_event("info", f"{container_name} geregistreerd (gestart: {current_start[:19]})")
                elif current_start != previous_start:
                    # Container has restarted
                    state["container_starts"][container_name] = current_start
                    add_event("warn", f"{container_name} herstart gedetecteerd — wacht {PLEX_RESTART_DELAY}s en herstart Plex")
                    threading.Timer(PLEX_RESTART_DELAY, restart_plex, args=[container_name]).start()

        except Exception as e:
            add_event("error", f"Watchdog fout: {e}")

        time.sleep(WATCHDOG_INTERVAL)


def restart_plex(trigger_container: str):
    try:
        plex = docker_client.containers.get(PLEX_CONTAINER)
        plex.restart()
        state["plex_restarts"] += 1
        add_event("success", f"Plex herstart na {trigger_container} restart (#{state['plex_restarts']})")
    except Exception as e:
        add_event("error", f"Plex herstart mislukt: {e}")


# ──────────────────────────────────────────────
# PlexTraktSync job
# ──────────────────────────────────────────────
def parse_sync_stats(output: str) -> dict:
    """Parse PlexTraktSync output for statistics."""
    import re
    stats = {
        "movies_synced": 0,
        "shows_synced": 0,
        "ratings_synced": 0,
        "watched_synced": 0,
    }
    # Count lines with sync actions
    for line in output.splitlines():
        l = line.lower()
        if "mark as watched" in l or "marked as watched" in l:
            stats["watched_synced"] += 1
        if "rating" in l and ("update" in l or "set" in l or "trakt" in l):
            stats["ratings_synced"] += 1
        if re.search(r'movie.*sync|sync.*movie', l):
            stats["movies_synced"] += 1
        if re.search(r'(show|episode|season).*sync|sync.*(show|episode)', l):
            stats["shows_synced"] += 1
    # Also look for summary lines like "Found X movies"
    m = re.search(r'(\d+)\s+movie', output, re.IGNORECASE)
    if m and stats["movies_synced"] == 0:
        stats["movies_synced"] = int(m.group(1))
    m = re.search(r'(\d+)\s+(?:show|episode)', output, re.IGNORECASE)
    if m and stats["shows_synced"] == 0:
        stats["shows_synced"] = int(m.group(1))
    return stats


def run_plextraktsync():
    if state["sync_status"] == "running":
        add_event("warn", "PlexTraktSync al actief — overgeslagen")
        return

    state["sync_status"] = "running"
    start_time = datetime.now()
    state["last_sync"] = start_time.strftime("%Y-%m-%d %H:%M:%S")
    add_event("info", "PlexTraktSync gestart")

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", f"{PLEXTRAKTSYNC_COMPOSE}/docker-compose.yml",
             "run", "--rm", PLEXTRAKTSYNC_CONTAINER, "sync"],
            capture_output=True, text=True, timeout=3600
        )
        duration = int((datetime.now() - start_time).total_seconds())
        combined_output = result.stdout + result.stderr

        if result.returncode == 0:
            state["sync_status"] = "success"
            stats = parse_sync_stats(combined_output)
            stats["duration"] = duration
            state["sync_stats"] = stats
            # Add to history (max 20)
            history_entry = {
                "time": state["last_sync"],
                "status": "success",
                "duration": duration,
                **stats,
            }
            state["sync_history"].insert(0, history_entry)
            state["sync_history"] = state["sync_history"][:20]
            add_event("success", f"PlexTraktSync voltooid in {duration}s — "
                      f"{stats['watched_synced']} watched, {stats['ratings_synced']} ratings")
        else:
            state["sync_status"] = "failed"
            state["sync_history"].insert(0, {
                "time": state["last_sync"], "status": "failed", "duration": duration
            })
            state["sync_history"] = state["sync_history"][:20]
            add_event("error", f"PlexTraktSync gefaald: {combined_output[-300:]}")
    except subprocess.TimeoutExpired:
        state["sync_status"] = "failed"
        add_event("error", "PlexTraktSync timeout (>1 uur)")
    except Exception as e:
        state["sync_status"] = "failed"
        add_event("error", f"PlexTraktSync fout: {e}")


# ──────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────
app = FastAPI(title="Watchdog Dashboard")


@app.get("/api/status")
def api_status():
    containers = [get_container_status(n) for n in WATCH_CONTAINERS + [PLEX_CONTAINER, PLEXTRAKTSYNC_CONTAINER]]
    return {
        "events": state["events"][:50],
        "sync_status": state["sync_status"],
        "last_sync": state["last_sync"],
        "next_sync": state["next_sync"],
        "plex_restarts": state["plex_restarts"],
        "containers": containers,
        "sync_stats": state["sync_stats"],
        "sync_history": state["sync_history"],
        "config": {
            "watch_containers": WATCH_CONTAINERS,
            "plex_restart_delay": PLEX_RESTART_DELAY,
            "sync_schedule": PLEXTRAKTSYNC_SCHEDULE,
        }
    }


@app.post("/api/sync/trigger")
def trigger_sync():
    threading.Thread(target=run_plextraktsync, daemon=True).start()
    return {"ok": True, "message": "PlexTraktSync handmatig gestart"}


@app.post("/api/plex/restart")
def trigger_plex_restart():
    threading.Thread(target=restart_plex, args=["manual"], daemon=True).start()
    return {"ok": True, "message": "Plex herstart handmatig gestart"}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML_TEMPLATE


# ──────────────────────────────────────────────
# Dashboard HTML
# ──────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Watchdog Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; }
  header { background: #1a1a2e; padding: 16px 24px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #333; }
  header h1 { font-size: 1.3rem; color: #e2b96f; }
  header span { font-size: 0.8rem; color: #888; margin-left: auto; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px; }
  .card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 16px; }
  .card h2 { font-size: 0.85rem; text-transform: uppercase; color: #888; margin-bottom: 12px; letter-spacing: 1px; }
  .containers { grid-column: 1 / -1; }
  .container-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .ct { background: #111; border-radius: 6px; padding: 10px 14px; display: flex; align-items: center; gap: 10px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .dot.running { background: #4caf50; box-shadow: 0 0 6px #4caf5088; }
  .dot.exited, .dot.stopped { background: #f44336; }
  .dot.not\\ found { background: #555; }
  .ct-name { font-size: 0.85rem; font-weight: 600; }
  .ct-since { font-size: 0.7rem; color: #666; margin-top: 2px; }
  .stat { text-align: center; padding: 8px; }
  .stat .val { font-size: 2rem; font-weight: 700; color: #e2b96f; }
  .stat .lbl { font-size: 0.75rem; color: #666; margin-top: 4px; }
  .stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .sync-badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; }
  .sync-badge.idle { background: #333; color: #aaa; }
  .sync-badge.running { background: #1565c0; color: #90caf9; }
  .sync-badge.success { background: #1b5e20; color: #a5d6a7; }
  .sync-badge.failed { background: #b71c1c; color: #ef9a9a; }
  .btn { background: #2a2a3e; border: 1px solid #444; color: #e0e0e0; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; transition: background 0.2s; }
  .btn:hover { background: #3a3a5e; }
  .btn.danger { border-color: #c62828; color: #ef9a9a; }
  .btn.danger:hover { background: #3e1010; }
  .btn-row { display: flex; gap: 8px; margin-top: 12px; }
  .events { grid-column: 1 / -1; }
  .event-list { max-height: 320px; overflow-y: auto; }
  .ev { display: flex; gap: 10px; padding: 6px 8px; border-bottom: 1px solid #1e1e1e; font-size: 0.8rem; align-items: flex-start; }
  .ev:hover { background: #1e1e1e; }
  .ev-time { color: #555; white-space: nowrap; flex-shrink: 0; }
  .ev-badge { flex-shrink: 0; padding: 1px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }
  .ev-badge.info { background: #1565c0; color: #90caf9; }
  .ev-badge.warn { background: #e65100; color: #ffcc80; }
  .ev-badge.error { background: #b71c1c; color: #ef9a9a; }
  .ev-badge.success { background: #1b5e20; color: #a5d6a7; }
  .ev-msg { color: #ccc; }
  .config-list { font-size: 0.8rem; line-height: 2; }
  .config-list span { color: #e2b96f; }
  .sync-stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 12px 0; }
  .sync-stat { background: #111; border-radius: 6px; padding: 8px; text-align: center; }
  .sync-stat .val { font-size: 1.4rem; font-weight: 700; color: #e2b96f; }
  .sync-stat .lbl { font-size: 0.7rem; color: #666; margin-top: 2px; }
  .history-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; margin-top: 8px; }
  .history-table th { color: #666; font-weight: 500; text-align: left; padding: 4px 6px; border-bottom: 1px solid #2a2a2a; }
  .history-table td { padding: 4px 6px; border-bottom: 1px solid #1a1a1a; color: #ccc; }
  .history-table tr:hover td { background: #1e1e1e; }
  .pill { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 0.7rem; font-weight: 600; }
  .pill.success { background: #1b5e20; color: #a5d6a7; }
  .pill.failed { background: #b71c1c; color: #ef9a9a; }
</style>
</head>
<body>
<header>
  <h1>🐺 Watchdog Dashboard</h1>
  <span id="last-update">laden...</span>
</header>
<div class="grid">
  <div class="card containers">
    <h2>Containers</h2>
    <div class="container-grid" id="containers"></div>
  </div>

  <div class="card">
    <h2>PlexTraktSync</h2>
    <div class="stats-grid">
      <div class="stat"><div class="val" id="sync-status-badge">—</div><div class="lbl">Status</div></div>
      <div class="stat"><div class="val" id="last-sync" style="font-size:0.9rem">—</div><div class="lbl">Laatste sync</div></div>
      <div class="stat"><div class="val" id="next-sync" style="font-size:0.9rem">—</div><div class="lbl">Volgende sync</div></div>
    </div>
    <div class="sync-stats-grid" id="sync-stats"></div>
    <div class="btn-row">
      <button class="btn" onclick="triggerSync()">▶ Nu synchen</button>
    </div>
  </div>

  <div class="card">
    <h2>Watchdog</h2>
    <div class="stats-grid">
      <div class="stat"><div class="val" id="plex-restarts">—</div><div class="lbl">Plex restarts</div></div>
    </div>
    <div class="config-list" id="config-info" style="margin-top:12px"></div>
    <div class="btn-row">
      <button class="btn danger" onclick="restartPlex()">↺ Plex herstarten</button>
    </div>
  </div>

  <div class="card events">
    <h2>Events log</h2>
    <div class="event-list" id="events"></div>
  </div>

  <div class="card" style="grid-column: 1 / -1;">
    <h2>Sync geschiedenis</h2>
    <table class="history-table">
      <thead><tr><th>Tijd</th><th>Status</th><th>Duur</th><th>Watched</th><th>Ratings</th><th>Films</th><th>Series</th></tr></thead>
      <tbody id="sync-history"></tbody>
    </table>
  </div>
</div>

<script>
async function fetchStatus() {
  const r = await fetch('/api/status');
  const d = await r.json();

  // Containers
  document.getElementById('containers').innerHTML = d.containers.map(c => {
    const since = c.started ? c.started.substring(0,19).replace('T',' ') : '';
    return `<div class="ct">
      <div class="dot ${c.status}"></div>
      <div><div class="ct-name">${c.name}</div><div class="ct-since">${since}</div></div>
    </div>`;
  }).join('');

  // Sync
  document.getElementById('sync-status-badge').innerHTML =
    `<span class="sync-badge ${d.sync_status}">${d.sync_status}</span>`;
  document.getElementById('last-sync').textContent = d.last_sync || '—';
  document.getElementById('next-sync').textContent = d.next_sync || '—';
  document.getElementById('plex-restarts').textContent = d.plex_restarts;

  // Sync stats
  const s = d.sync_stats;
  const statsItems = [
    { val: s.watched_synced ?? '—', lbl: 'Watched' },
    { val: s.ratings_synced ?? '—', lbl: 'Ratings' },
    { val: s.movies_synced ?? '—', lbl: 'Films' },
    { val: s.shows_synced ?? '—', lbl: 'Series' },
  ];
  document.getElementById('sync-stats').innerHTML = statsItems.map(i =>
    `<div class="sync-stat"><div class="val">${i.val}</div><div class="lbl">${i.lbl}</div></div>`
  ).join('');

  // Sync history
  document.getElementById('sync-history').innerHTML = (d.sync_history || []).map(h =>
    `<tr>
      <td>${h.time}</td>
      <td><span class="pill ${h.status}">${h.status}</span></td>
      <td>${h.duration ? h.duration + 's' : '—'}</td>
      <td>${h.watched_synced ?? '—'}</td>
      <td>${h.ratings_synced ?? '—'}</td>
      <td>${h.movies_synced ?? '—'}</td>
      <td>${h.shows_synced ?? '—'}</td>
    </tr>`
  ).join('') || '<tr><td colspan="7" style="color:#555;text-align:center;padding:12px">Nog geen syncs uitgevoerd</td></tr>';

  // Config
  document.getElementById('config-info').innerHTML = `
    Bewaakt: <span>${d.config.watch_containers.join(', ')}</span><br>
    Restart delay: <span>${d.config.plex_restart_delay}s</span><br>
    Sync schema: <span>${d.config.sync_schedule}</span>
  `;

  // Events
  document.getElementById('events').innerHTML = d.events.map(e =>
    `<div class="ev">
      <span class="ev-time">${e.time}</span>
      <span class="ev-badge ${e.type}">${e.type}</span>
      <span class="ev-msg">${e.message}</span>
    </div>`
  ).join('');

  document.getElementById('last-update').textContent = 'bijgewerkt: ' + new Date().toLocaleTimeString('nl-NL');
}

async function triggerSync() {
  await fetch('/api/sync/trigger', {method:'POST'});
  setTimeout(fetchStatus, 1000);
}

async function restartPlex() {
  if (!confirm('Plex herstarten?')) return;
  await fetch('/api/plex/restart', {method:'POST'});
  setTimeout(fetchStatus, 2000);
}

fetchStatus();
setInterval(fetchStatus, 15000);
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────
def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Europe/Amsterdam")
    hour, minute = "6,18", "0"
    scheduler.add_job(
        run_plextraktsync,
        CronTrigger(hour="6,18", minute="0", timezone="Europe/Amsterdam"),
        id="plextraktsync",
        name="PlexTraktSync",
    )
    scheduler.start()
    # Update next_sync in state
    def update_next():
        while True:
            job = scheduler.get_job("plextraktsync")
            if job and job.next_run_time:
                state["next_sync"] = job.next_run_time.strftime("%Y-%m-%d %H:%M")
            time.sleep(60)
    threading.Thread(target=update_next, daemon=True).start()
    add_event("info", f"Scheduler gestart — PlexTraktSync om 06:00 en 18:00")


if __name__ == "__main__":
    threading.Thread(target=watchdog_loop, daemon=True).start()
    start_scheduler()
    uvicorn.run(app, host="0.0.0.0", port=8090)

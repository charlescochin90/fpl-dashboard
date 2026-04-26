#!/usr/bin/env python3
"""
FPL Classic League Dashboard Builder (v2)
==========================================
Fetches data for a given FPL classic league and generates a static
index.html dashboard with:
  - League standings (horizontal bar)
  - Form guide (last 4 GWs)
  - Point margin (gap to team directly below)
  - Cumulative points ahead of last place (line)
  - Player contributions (top 5 players + Others, stacked %)
  - Points left on bench (vertical bar)
  - Top transfers in (best buys of the season, league-wide)
  - Weekly performance (small multiples, one mini bar chart per manager)

Designed to run in GitHub Actions on a schedule.

Usage:
    python build.py --league-id 2007916 --output index.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FPL_API = "https://fantasy.premierleague.com/api"
USER_AGENT = "Mozilla/5.0 (compatible; fpl-dashboard/2.0)"

# Per-manager colour palette, applied in standings order. Inspired by
# the warm/distinct palette in popular community dashboards.
PALETTE = [
    "#84cc16",  # lime green
    "#8b5cf6",  # purple
    "#b45459",  # warm red/brown
    "#f59e0b",  # orange
    "#f0a3b6",  # pink
    "#eab308",  # yellow
    "#0ea5e9",  # sky blue
    "#ef4444",  # red
    "#10b981",  # emerald
    "#6366f1",  # indigo
    "#14b8a6",  # teal
    "#f97316",  # orange-deep
]


# --------------------------------------------------------------------------
# Networking helpers
# --------------------------------------------------------------------------

def fetch_json(url: str, retries: int = 3, sleep: float = 0.05) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                time.sleep(sleep)
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            print(f"  retry {attempt + 1}/{retries} for {url}: {e}", file=sys.stderr)
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def fetch_league(league_id: int) -> dict:
    return fetch_json(f"{FPL_API}/leagues-classic/{league_id}/standings/")


def fetch_history(entry_id: int) -> dict:
    return fetch_json(f"{FPL_API}/entry/{entry_id}/history/")


def fetch_transfers(entry_id: int) -> list:
    return fetch_json(f"{FPL_API}/entry/{entry_id}/transfers/")


def fetch_picks(entry_id: int, gw: int) -> dict:
    return fetch_json(f"{FPL_API}/entry/{entry_id}/event/{gw}/picks/")


def fetch_bootstrap() -> dict:
    return fetch_json(f"{FPL_API}/bootstrap-static/")


def fetch_live(gw: int) -> dict:
    return fetch_json(f"{FPL_API}/event/{gw}/live/")


# --------------------------------------------------------------------------
# Data construction
# --------------------------------------------------------------------------

def build_manager(
    row: dict,
    finished_gws: list[int],
    live_by_gw: dict[int, dict[int, int]],
    elements: dict[int, dict],
) -> dict:
    entry_id = row["entry"]
    print(f"  fetching for {row['player_name']} ({entry_id})...")

    history = fetch_history(entry_id)
    transfers = fetch_transfers(entry_id)

    gw_history = history.get("current", [])
    picks_by_gw: dict[int, dict] = {}
    for gw in finished_gws:
        try:
            picks_by_gw[gw] = fetch_picks(entry_id, gw)
        except RuntimeError:
            # Manager may have entered late — skip missing GWs gracefully.
            picks_by_gw[gw] = {"picks": []}

    # Player contributions: sum effective points (mult * raw points) across the
    # season for every player who appeared in this manager's lineup.
    contrib: dict[int, int] = {}
    for gw, picks_data in picks_by_gw.items():
        live = live_by_gw.get(gw, {})
        for pick in picks_data.get("picks", []):
            mult = pick.get("multiplier", 0)
            if mult <= 0:
                continue  # benched and no auto-sub
            pid = pick["element"]
            pts = live.get(pid, 0) * mult
            contrib[pid] = contrib.get(pid, 0) + pts

    total_contrib = sum(contrib.values()) or 1
    sorted_players = sorted(contrib.items(), key=lambda kv: -kv[1])
    top_players = []
    for pid, pts in sorted_players[:5]:
        top_players.append({
            "name": elements[pid]["web_name"] if pid in elements else f"#{pid}",
            "points": int(pts),
            "share": round(100 * pts / total_contrib, 2),
        })
    others_pts = sum(p for _, p in sorted_players[5:])
    if others_pts > 0:
        top_players.append({
            "name": "Others",
            "points": int(others_pts),
            "share": round(100 * others_pts / total_contrib, 2),
        })

    # Compute transfer-in records: for each transfer-in, how many points has the
    # incoming player scored *while owned* by this manager (until the next sale,
    # or end of season).
    transfer_records = compute_transfer_records(
        transfers, finished_gws, live_by_gw, elements
    )

    return {
        "entry_id": entry_id,
        "player_name": row["player_name"],
        "team_name": row["entry_name"],
        "rank": row["rank"],
        "last_rank": row["last_rank"],
        "total_points": row["total"],
        "event_total": row["event_total"],
        "gameweeks": [
            {
                "gw": g["event"],
                "points": g["points"],
                "total_points": g["total_points"],
                "bench": g.get("points_on_bench", 0),
                "transfers": g.get("event_transfers", 0),
                "transfer_cost": g.get("event_transfers_cost", 0),
            }
            for g in gw_history
        ],
        "top_players": top_players,
        "transfers_in": transfer_records,
    }


def compute_transfer_records(
    transfers: list,
    finished_gws: list[int],
    live_by_gw: dict[int, dict[int, int]],
    elements: dict[int, dict],
) -> list[dict]:
    """For each transfer-in, sum the player's raw points during ownership."""
    # Group transfers chronologically per (player_in).
    # Ownership window: player_in at gw=in; window closes if the SAME player is
    # transferred OUT later (they appear as element_out in a subsequent record).
    if not transfers:
        return []

    transfers_sorted = sorted(transfers, key=lambda t: t.get("event", 0))
    records: list[dict] = []
    last_finished = max(finished_gws) if finished_gws else 0

    for i, t in enumerate(transfers_sorted):
        pid_in = t["element_in"]
        gw_in = t["event"]
        # Find the next time this player is transferred OUT after gw_in.
        gw_out = None
        for later in transfers_sorted[i + 1:]:
            if later["element_out"] == pid_in and later["event"] >= gw_in:
                gw_out = later["event"]
                break
        end_gw = (gw_out - 1) if gw_out is not None else last_finished
        pts = 0
        for gw in range(gw_in, end_gw + 1):
            pts += live_by_gw.get(gw, {}).get(pid_in, 0)
        records.append({
            "player_name": elements[pid_in]["web_name"] if pid_in in elements else f"#{pid_in}",
            "gw_in": gw_in,
            "points_earned": pts,
        })
    return records


def build_dataset(league_id: int, override_name: str | None = None) -> dict:
    print(f"Fetching league {league_id}...")
    league = fetch_league(league_id)
    bootstrap = fetch_bootstrap()

    events = bootstrap.get("events", [])
    finished_gws = [e["id"] for e in events if e.get("finished")]
    current = next((e["id"] for e in events if e.get("is_current")), None)
    current_gw = current or (finished_gws[-1] if finished_gws else 0)

    elements = {p["id"]: p for p in bootstrap.get("elements", [])}

    print(f"Fetching live data for {len(finished_gws)} finished GWs...")
    live_by_gw: dict[int, dict[int, int]] = {}
    for gw in finished_gws:
        live = fetch_live(gw)
        live_by_gw[gw] = {
            el["id"]: el.get("stats", {}).get("total_points", 0)
            for el in live.get("elements", [])
        }

    standings = league.get("standings", {}).get("results", [])
    league_name = override_name or league["league"]["name"]
    print(f"League: {league_name} ({len(standings)} managers, GW {current_gw})")

    managers = []
    for idx, row in enumerate(standings):
        try:
            m = build_manager(row, finished_gws, live_by_gw, elements)
            m["color"] = PALETTE[idx % len(PALETTE)]
            managers.append(m)
        except RuntimeError as e:
            print(f"  skipping {row['player_name']}: {e}", file=sys.stderr)

    # League-wide best transfers in (top 20).
    color_by_mgr = {m["player_name"]: m["color"] for m in managers}
    flat: list[dict] = []
    for m in managers:
        for t in m["transfers_in"]:
            flat.append({
                "player_name": t["player_name"],
                "manager_name": m["player_name"],
                "color": color_by_mgr[m["player_name"]],
                "points_earned": t["points_earned"],
                "gw_in": t["gw_in"],
            })
    flat.sort(key=lambda x: -x["points_earned"])
    top_transfers = flat[:20]

    return {
        "league": {
            "id": league["league"]["id"],
            "name": league_name,
        },
        "current_gw": current_gw,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "managers": managers,
        "top_transfers": top_transfers,
    }


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__LEAGUE_NAME__ — FPL Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<style>
  :root {
    --bg: #f6f7fb;
    --panel: #ffffff;
    --panel-2: #f0f2f6;
    --line: #e4e7ee;
    --text: #1a1f2e;
    --muted: #6b7280;
    --accent: #ef4444;
    --accent-2: #2563eb;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 28px 20px 80px; }
  header {
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    margin-bottom: 28px;
  }
  .logo {
    display: inline-flex; align-items: center; gap: 12px;
    background: #ef4444; color: #fff; padding: 12px 24px; border-radius: 999px;
    font-size: 26px; font-weight: 800; letter-spacing: -0.01em;
    box-shadow: 0 4px 14px rgba(239, 68, 68, 0.25);
  }
  .logo .flag { font-size: 28px; }
  .logo .pill {
    background: #facc15; color: #422006; padding: 3px 10px; border-radius: 6px;
    font-size: 13px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
  }
  .sub { color: var(--muted); font-size: 13px; }
  .grid { display: grid; gap: 20px; grid-template-columns: 1fr; margin-bottom: 20px; }
  @media (min-width: 760px) { .g3 { grid-template-columns: repeat(3, 1fr); } }
  @media (min-width: 760px) { .g2 { grid-template-columns: repeat(2, 1fr); } }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; padding: 18px 20px;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  }
  .card h2 {
    margin: 0 0 14px; font-size: 16px; font-weight: 700; letter-spacing: -0.01em;
  }
  .chart-box { position: relative; height: 280px; }
  .chart-box.tall { height: 360px; }
  .chart-box.short { height: 220px; }
  .small-multiples { display: grid; gap: 16px; grid-template-columns: 1fr; }
  @media (min-width: 720px) { .small-multiples { grid-template-columns: repeat(2, 1fr); } }
  @media (min-width: 1080px) { .small-multiples { grid-template-columns: repeat(3, 1fr); } }
  .mini {
    display: grid; grid-template-columns: 110px 1fr; align-items: center; gap: 10px;
  }
  .mini .name {
    font-weight: 600; font-size: 13px; color: var(--text);
    overflow: hidden; text-overflow: ellipsis;
  }
  .mini .canvas-host { position: relative; height: 90px; }
  footer {
    margin-top: 40px; color: var(--muted); font-size: 12px; text-align: center;
  }
  footer a { color: var(--accent-2); text-decoration: none; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">
      <span class="flag">🇨🇭</span>
      <span id="league-name-text">League</span>
      <span class="pill">Dashboard</span>
    </div>
    <div class="sub">
      <span id="badge-gw">GW –</span> ·
      <span id="badge-count">– managers</span> ·
      Updated <span id="badge-updated">–</span>
    </div>
  </header>

  <section class="grid g3">
    <div class="card">
      <h2>League Standings</h2>
      <div class="chart-box"><canvas id="chart-standings"></canvas></div>
    </div>
    <div class="card">
      <h2>Form Guide (Last 4 GWs)</h2>
      <div class="chart-box"><canvas id="chart-form"></canvas></div>
    </div>
    <div class="card">
      <h2>Point Margin</h2>
      <div class="chart-box"><canvas id="chart-margin"></canvas></div>
    </div>
  </section>

  <section class="grid g2">
    <div class="card">
      <h2>Cumulative Points (ahead of last)</h2>
      <div class="chart-box tall"><canvas id="chart-cumulative"></canvas></div>
    </div>
    <div class="card">
      <h2>Player Contributions</h2>
      <div class="chart-box tall"><canvas id="chart-contrib"></canvas></div>
    </div>
  </section>

  <section class="grid g2">
    <div class="card">
      <h2>Points Left on Bench</h2>
      <div class="chart-box"><canvas id="chart-bench"></canvas></div>
    </div>
    <div class="card">
      <h2>Top Transfers In</h2>
      <div class="chart-box tall"><canvas id="chart-transfers"></canvas></div>
    </div>
  </section>

  <section class="card">
    <h2>Weekly Performance</h2>
    <div class="small-multiples" id="small-multiples"></div>
  </section>

  <footer>
    Auto-rebuilt from the
    <a href="https://fantasy.premierleague.com/api/leagues-classic/__LEAGUE_ID__/standings/" target="_blank">FPL public API</a>.
    Data: Premier League / FPL.
  </footer>
</div>

<script id="dashboard-data" type="application/json">__DATA_JSON__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById('dashboard-data').textContent);
  Chart.register(ChartDataLabels);
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif';
  Chart.defaults.color = '#334155';
  Chart.defaults.plugins.datalabels.display = false;

  // --- header ---
  document.title = data.league.name + ' — FPL Dashboard';
  document.getElementById('league-name-text').textContent = data.league.name;
  document.getElementById('badge-gw').textContent = 'GW ' + data.current_gw;
  document.getElementById('badge-count').textContent = data.managers.length + ' managers';
  const upd = new Date(data.generated_at);
  document.getElementById('badge-updated').textContent = upd.toLocaleString(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  });

  const byRank = data.managers.slice().sort((a, b) => a.rank - b.rank);
  const colorByName = {};
  data.managers.forEach((m) => (colorByName[m.player_name] = m.color));

  // ---- 1. Standings ----
  hbar('chart-standings', {
    labels: byRank.map((m) => shortName(m.team_name || m.player_name)),
    values: byRank.map((m) => m.total_points),
    colors: byRank.map((m) => m.color),
    showValues: true,
  });

  // ---- 2. Form (last 4 GW points) ----
  const formData = data.managers.map((m) => {
    const last4 = m.gameweeks.slice(-4);
    return {
      name: shortName(m.team_name || m.player_name),
      color: m.color,
      total: last4.reduce((s, g) => s + g.points, 0),
    };
  }).sort((a, b) => b.total - a.total);
  hbar('chart-form', {
    labels: formData.map((d) => d.name),
    values: formData.map((d) => d.total),
    colors: formData.map((d) => d.color),
    showValues: true,
  });

  // ---- 3. Point Margin (gap to team directly below) ----
  const marginData = byRank.map((m, i) => {
    const next = byRank[i + 1];
    return {
      name: shortName(m.team_name || m.player_name),
      color: m.color,
      gap: next ? m.total_points - next.total_points : 0,
    };
  });
  hbar('chart-margin', {
    labels: marginData.map((d) => d.name),
    values: marginData.map((d) => d.gap),
    colors: marginData.map((d) => d.color),
    showValues: true,
  });

  // ---- 4. Cumulative points ahead of last ----
  const allGws = collectGws(data.managers);
  // For each GW, find the lowest cumulative total across managers.
  const lowestByGw = {};
  allGws.forEach((gw) => {
    const totals = data.managers.map((m) => {
      const r = m.gameweeks.find((g) => g.gw === gw);
      return r ? r.total_points : null;
    }).filter((v) => v !== null);
    lowestByGw[gw] = totals.length ? Math.min(...totals) : 0;
  });
  new Chart(document.getElementById('chart-cumulative'), {
    type: 'line',
    data: {
      labels: allGws.map((g) => g),
      datasets: data.managers.map((m) => ({
        label: shortName(m.team_name || m.player_name),
        data: allGws.map((gw) => {
          const r = m.gameweeks.find((g) => g.gw === gw);
          return r ? r.total_points - lowestByGw[gw] : null;
        }),
        borderColor: m.color,
        backgroundColor: m.color + '22',
        borderWidth: 2,
        tension: 0.2,
        pointRadius: 2,
        pointHoverRadius: 5,
        spanGaps: true,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 10, usePointStyle: true } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: { title: { display: true, text: 'Gameweek' }, grid: { color: '#eef0f5' } },
        y: { title: { display: true, text: 'Points ahead of last' }, grid: { color: '#eef0f5' } },
      },
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
    },
  });

  // ---- 5. Player Contributions (stacked horizontal, %) ----
  const contribLabels = data.managers.map((m) => shortName(m.team_name || m.player_name));
  // Each manager has up to 5 named players + "Others" (variable size).
  // Build datasets per slot index so each segment renders as its own dataset.
  const maxSlots = Math.max(...data.managers.map((m) => m.top_players.length));
  const contribDatasets = [];
  for (let slot = 0; slot < maxSlots; slot++) {
    contribDatasets.push({
      label: 'Slot ' + (slot + 1),
      data: data.managers.map((m) => (m.top_players[slot] ? m.top_players[slot].share : 0)),
      backgroundColor: data.managers.map((m) => {
        const base = m.color;
        const isOthers = m.top_players[slot] && m.top_players[slot].name === 'Others';
        return isOthers ? base + '55' : shadeColor(base, slot * -8);
      }),
      borderWidth: 0,
      datalabels: {
        display: (ctx) => {
          const m = data.managers[ctx.dataIndex];
          const p = m.top_players[slot];
          return p && ctx.dataset.data[ctx.dataIndex] >= 6;
        },
        formatter: (_, ctx) => {
          const m = data.managers[ctx.dataIndex];
          const p = m.top_players[slot];
          return p ? p.name : '';
        },
        color: '#fff',
        font: { weight: 600, size: 11 },
        anchor: 'center', align: 'center',
      },
    });
  }
  new Chart(document.getElementById('chart-contrib'), {
    type: 'bar',
    data: { labels: contribLabels, datasets: contribDatasets },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const m = data.managers[ctx.dataIndex];
              const slot = ctx.datasetIndex;
              const p = m.top_players[slot];
              return p ? `${p.name}: ${p.points} pts (${p.share}%)` : '';
            },
          },
        },
      },
      scales: {
        x: { stacked: true, max: 100, ticks: { callback: (v) => v + '%' }, grid: { color: '#eef0f5' } },
        y: { stacked: true, grid: { display: false } },
      },
    },
  });

  // ---- 6. Points Left on Bench (totals) ----
  const benchData = data.managers.map((m) => ({
    name: shortName(m.team_name || m.player_name),
    color: m.color,
    total: m.gameweeks.reduce((s, g) => s + (g.bench || 0), 0),
  })).sort((a, b) => b.total - a.total);
  new Chart(document.getElementById('chart-bench'), {
    type: 'bar',
    data: {
      labels: benchData.map((d) => d.name),
      datasets: [{
        data: benchData.map((d) => d.total),
        backgroundColor: benchData.map((d) => d.color),
        borderRadius: 6,
        datalabels: {
          display: true, anchor: 'end', align: 'top',
          color: '#1a1f2e', font: { weight: 700 },
        },
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true, grid: { color: '#eef0f5' } },
      },
    },
  });

  // ---- 7. Top Transfers In (horizontal bar, coloured by manager) ----
  const tt = data.top_transfers || [];
  new Chart(document.getElementById('chart-transfers'), {
    type: 'bar',
    data: {
      labels: tt.map((t) => t.player_name),
      datasets: [{
        data: tt.map((t) => t.points_earned),
        backgroundColor: tt.map((t) => t.color),
        borderRadius: 4,
      }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const t = tt[ctx.dataIndex];
              return `${t.points_earned} pts · ${t.manager_name} · GW ${t.gw_in}`;
            },
          },
        },
      },
      scales: {
        x: { beginAtZero: true, grid: { color: '#eef0f5' } },
        y: { grid: { display: false } },
      },
    },
  });

  // ---- 8. Weekly Performance (small multiples) ----
  const host = document.getElementById('small-multiples');
  data.managers.forEach((m) => {
    const row = document.createElement('div');
    row.className = 'mini';
    row.innerHTML = `<div class="name" title="${escapeHtml(m.player_name)}">${escapeHtml(shortName(m.team_name || m.player_name))}</div><div class="canvas-host"><canvas></canvas></div>`;
    host.appendChild(row);
    const canvas = row.querySelector('canvas');
    new Chart(canvas, {
      type: 'bar',
      data: {
        labels: m.gameweeks.map((g) => g.gw),
        datasets: [{
          data: m.gameweeks.map((g) => g.points),
          backgroundColor: m.color,
          borderRadius: 2,
          barPercentage: 0.85,
          categoryPercentage: 1,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: {
          callbacks: { title: (ctx) => 'GW ' + ctx[0].label, label: (ctx) => ctx.raw + ' pts' },
        }},
        scales: {
          x: { display: false, grid: { display: false } },
          y: { beginAtZero: true, suggestedMax: 80, ticks: { stepSize: 40, font: { size: 10 } }, grid: { color: '#eef0f5' } },
        },
      },
    });
  });

  // ---- helpers ----
  function hbar(canvasId, { labels, values, colors, showValues }) {
    new Chart(document.getElementById(canvasId), {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: colors,
          borderRadius: 4,
          datalabels: showValues ? {
            display: true, anchor: 'end', align: 'right', clamp: true,
            color: '#1a1f2e', font: { weight: 700, size: 11 },
          } : { display: false },
        }],
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        layout: { padding: { right: 36 } },
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, grid: { color: '#eef0f5' } },
          y: { grid: { display: false } },
        },
      },
    });
  }

  function collectGws(managers) {
    const set = new Set();
    managers.forEach((m) => m.gameweeks.forEach((g) => set.add(g.gw)));
    return Array.from(set).sort((a, b) => a - b);
  }

  function shortName(s) {
    return s && s.length > 18 ? s.slice(0, 17) + '…' : s;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function shadeColor(hex, percent) {
    const m = hex.replace('#', '');
    const num = parseInt(m, 16);
    let r = (num >> 16) + percent;
    let g = ((num >> 8) & 0xff) + percent;
    let b = (num & 0xff) + percent;
    r = Math.max(0, Math.min(255, r));
    g = Math.max(0, Math.min(255, g));
    b = Math.max(0, Math.min(255, b));
    return '#' + ((r << 16) | (g << 8) | b).toString(16).padStart(6, '0');
  }
})();
</script>
</body>
</html>
"""


def render_html(dataset: dict) -> str:
    league_name = dataset["league"]["name"]
    league_id = dataset["league"]["id"]
    payload = json.dumps(dataset, ensure_ascii=False).replace("</", "<\\/")
    return (
        HTML_TEMPLATE
        .replace("__LEAGUE_NAME__", html_escape(league_name))
        .replace("__LEAGUE_ID__", str(league_id))
        .replace("__DATA_JSON__", payload)
    )


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Build an FPL classic league dashboard.")
    p.add_argument("--league-id", type=int, required=True)
    p.add_argument("--output", type=Path, default=Path("index.html"))
    p.add_argument("--league-name", type=str, default=None,
                   help="Override the league name shown in the header.")
    args = p.parse_args()

    dataset = build_dataset(args.league_id, override_name=args.league_name)
    html = render_html(dataset)
    args.output.write_text(html, encoding="utf-8")
    print(f"Wrote {args.output} ({len(html):,} bytes, {len(dataset['managers'])} managers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

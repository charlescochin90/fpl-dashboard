#!/usr/bin/env python3
"""
FPL League Dashboard Builder
=============================
Fetches data for a given FPL classic league and generates a static
index.html dashboard. Designed to run in GitHub Actions on a schedule.

Usage:
    python build.py --league-id 2007916 --output index.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

FPL_API = "https://fantasy.premierleague.com/api"
USER_AGENT = "Mozilla/5.0 (compatible; fpl-dashboard/1.0)"


def fetch_json(url: str, retries: int = 3) -> dict:
    """Fetch JSON from a URL with retries and a friendly user agent."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            print(f"  retry {attempt + 1}/{retries} for {url}: {e}", file=sys.stderr)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def fetch_league(league_id: int) -> dict:
    return fetch_json(f"{FPL_API}/leagues-classic/{league_id}/standings/")


def fetch_history(entry_id: int) -> dict:
    return fetch_json(f"{FPL_API}/entry/{entry_id}/history/")


def fetch_bootstrap() -> dict:
    return fetch_json(f"{FPL_API}/bootstrap-static/")


def build_dataset(league_id: int) -> dict:
    """Pull everything needed and shape it for the dashboard."""
    print(f"Fetching league {league_id}...")
    league = fetch_league(league_id)
    bootstrap = fetch_bootstrap()

    events = bootstrap.get("events", [])
    current_gw = next((e["id"] for e in events if e.get("is_current")), None)
    if current_gw is None:
        finished = [e for e in events if e.get("finished")]
        current_gw = finished[-1]["id"] if finished else 0

    standings = league.get("standings", {}).get("results", [])
    print(f"League: {league['league']['name']} ({len(standings)} managers, GW {current_gw})")

    managers = []
    for row in standings:
        entry_id = row["entry"]
        print(f"  fetching history for {row['player_name']} ({entry_id})...")
        try:
            hist = fetch_history(entry_id)
        except RuntimeError as e:
            print(f"    skipping: {e}", file=sys.stderr)
            continue
        gw_history = hist.get("current", [])
        managers.append({
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
                    "rank": g.get("rank"),
                    "overall_rank": g.get("overall_rank"),
                    "bench": g.get("points_on_bench", 0),
                    "transfers": g.get("event_transfers", 0),
                    "transfer_cost": g.get("event_transfers_cost", 0),
                }
                for g in gw_history
            ],
        })

    return {
        "league": {
            "id": league["league"]["id"],
            "name": league["league"]["name"],
        },
        "current_gw": current_gw,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "managers": managers,
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
<style>
  :root {
    --bg: #0b0e16;
    --panel: #131826;
    --panel-2: #1a2030;
    --line: #232a3d;
    --text: #e6e9f2;
    --muted: #8a93a8;
    --accent: #37d4a3;
    --accent-2: #6ea8ff;
    --warn: #ffb454;
    --bad: #ff6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 32px 20px 80px; }
  header {
    display: flex; align-items: flex-end; justify-content: space-between;
    flex-wrap: wrap; gap: 16px; margin-bottom: 28px;
    border-bottom: 1px solid var(--line); padding-bottom: 20px;
  }
  h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }
  .sub { color: var(--muted); font-size: 14px; margin-top: 4px; }
  .badges { display: flex; gap: 8px; flex-wrap: wrap; }
  .badge {
    background: var(--panel); border: 1px solid var(--line);
    padding: 6px 12px; border-radius: 999px; font-size: 13px; color: var(--muted);
  }
  .badge strong { color: var(--text); font-weight: 600; }
  .grid { display: grid; gap: 20px; grid-template-columns: 1fr; }
  @media (min-width: 900px) { .grid-2 { grid-template-columns: 1fr 1fr; } }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; padding: 20px;
  }
  .card h2 {
    margin: 0 0 4px; font-size: 16px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted);
  }
  .card .desc { font-size: 13px; color: var(--muted); margin-bottom: 16px; }
  .chart-box { position: relative; height: 320px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); }
  th { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .rank { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
  .move { font-size: 12px; }
  .move.up { color: var(--accent); }
  .move.down { color: var(--bad); }
  .move.same { color: var(--muted); }
  .stat-grid { display: grid; gap: 12px; grid-template-columns: repeat(2, 1fr); }
  @media (min-width: 700px) { .stat-grid { grid-template-columns: repeat(4, 1fr); } }
  .stat {
    background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px;
    padding: 14px;
  }
  .stat .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
  .stat .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
  .stat .who { font-size: 13px; color: var(--muted); margin-top: 2px; }
  footer { margin-top: 36px; color: var(--muted); font-size: 12px; text-align: center; }
  footer a { color: var(--accent-2); text-decoration: none; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1 id="league-name">FPL League</h1>
      <div class="sub" id="league-sub">Loading…</div>
    </div>
    <div class="badges">
      <div class="badge">GW <strong id="badge-gw">–</strong></div>
      <div class="badge">Managers <strong id="badge-count">–</strong></div>
      <div class="badge">Updated <strong id="badge-updated">–</strong></div>
    </div>
  </header>

  <section class="card" style="margin-bottom: 20px;">
    <h2>Hall of Fame &amp; Shame</h2>
    <div class="desc">Season-defining moments. Don't bring these up unless you want a fight.</div>
    <div class="stat-grid" id="stats"></div>
  </section>

  <section class="grid grid-2" style="margin-bottom: 20px;">
    <div class="card">
      <h2>Total points trajectory</h2>
      <div class="desc">Cumulative points across the season.</div>
      <div class="chart-box"><canvas id="chart-total"></canvas></div>
    </div>
    <div class="card">
      <h2>Gameweek points (last 10)</h2>
      <div class="desc">Recent form. Higher = on a heater.</div>
      <div class="chart-box"><canvas id="chart-gw"></canvas></div>
    </div>
  </section>

  <section class="card">
    <h2>Standings</h2>
    <div class="desc">Live league table.</div>
    <div style="overflow-x:auto;">
      <table id="standings">
        <thead>
          <tr>
            <th>#</th>
            <th>Manager</th>
            <th>Team</th>
            <th class="num">GW</th>
            <th class="num">Total</th>
            <th class="num">Bench (GW)</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <footer>
    Auto-rebuilt from <a href="https://fantasy.premierleague.com/api/leagues-classic/__LEAGUE_ID__/standings/" target="_blank">FPL public API</a>.
    Data &amp; charts: Premier League / FPL.
  </footer>
</div>

<script id="dashboard-data" type="application/json">__DATA_JSON__</script>
<script>
(() => {
  const raw = document.getElementById('dashboard-data').textContent;
  const data = JSON.parse(raw);

  // --- header ---
  document.title = data.league.name + ' — FPL Dashboard';
  document.getElementById('league-name').textContent = data.league.name;
  document.getElementById('league-sub').textContent =
    'Season tracker for league #' + data.league.id;
  document.getElementById('badge-gw').textContent = data.current_gw;
  document.getElementById('badge-count').textContent = data.managers.length;
  const upd = new Date(data.generated_at);
  document.getElementById('badge-updated').textContent =
    upd.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

  // --- standings ---
  const tbody = document.querySelector('#standings tbody');
  data.managers
    .slice()
    .sort((a, b) => a.rank - b.rank)
    .forEach((m) => {
      const tr = document.createElement('tr');
      const move = m.last_rank === 0 ? 0 : m.last_rank - m.rank;
      const moveCls = move > 0 ? 'up' : move < 0 ? 'down' : 'same';
      const moveSym = move > 0 ? '▲ ' + move : move < 0 ? '▼ ' + Math.abs(move) : '–';
      tr.innerHTML = `
        <td><span class="rank">${m.rank}<span class="move ${moveCls}">${moveSym}</span></span></td>
        <td>${escapeHtml(m.player_name)}</td>
        <td style="color:var(--muted)">${escapeHtml(m.team_name)}</td>
        <td class="num">${m.event_total ?? '–'}</td>
        <td class="num"><strong>${m.total_points}</strong></td>
        <td class="num">${lastBench(m)}</td>
      `;
      tbody.appendChild(tr);
    });

  // --- charts ---
  const palette = ['#37d4a3','#6ea8ff','#ffb454','#ff6b6b','#b388ff','#ffd166','#43c6ac','#f78fb3','#7bdcb5','#fc9d9a','#a0c4ff','#bdb2ff'];
  const allGws = collectGws(data.managers);

  // Total points trajectory
  new Chart(document.getElementById('chart-total'), {
    type: 'line',
    data: {
      labels: allGws.map((g) => 'GW ' + g),
      datasets: data.managers.map((m, i) => ({
        label: m.player_name,
        data: allGws.map((gw) => {
          const row = m.gameweeks.find((g) => g.gw === gw);
          return row ? row.total_points : null;
        }),
        borderColor: palette[i % palette.length],
        backgroundColor: palette[i % palette.length] + '22',
        tension: 0.25,
        spanGaps: true,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
      })),
    },
    options: chartOptions({ stacked: false }),
  });

  // Last-10 GW points
  const last10 = allGws.slice(-10);
  new Chart(document.getElementById('chart-gw'), {
    type: 'line',
    data: {
      labels: last10.map((g) => 'GW ' + g),
      datasets: data.managers.map((m, i) => ({
        label: m.player_name,
        data: last10.map((gw) => {
          const row = m.gameweeks.find((g) => g.gw === gw);
          return row ? row.points : null;
        }),
        borderColor: palette[i % palette.length],
        backgroundColor: palette[i % palette.length] + '22',
        tension: 0.25,
        spanGaps: true,
        pointRadius: 3,
        borderWidth: 2,
      })),
    },
    options: chartOptions({ stacked: false }),
  });

  // --- stats ---
  renderStats(data.managers);

  // ---- helpers ----
  function chartOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#e6e9f2', usePointStyle: true, boxWidth: 8 } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: { grid: { color: '#232a3d' }, ticks: { color: '#8a93a8' } },
        y: { grid: { color: '#232a3d' }, ticks: { color: '#8a93a8' } },
      },
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
    };
  }

  function collectGws(managers) {
    const set = new Set();
    managers.forEach((m) => m.gameweeks.forEach((g) => set.add(g.gw)));
    return Array.from(set).sort((a, b) => a - b);
  }

  function lastBench(m) {
    const last = m.gameweeks[m.gameweeks.length - 1];
    return last ? last.bench : '–';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function renderStats(managers) {
    const flat = [];
    managers.forEach((m) =>
      m.gameweeks.forEach((g) =>
        flat.push({ ...g, name: m.player_name, team: m.team_name })
      )
    );
    if (flat.length === 0) return;

    const highest = flat.reduce((a, b) => (b.points > a.points ? b : a));
    const lowest = flat.reduce((a, b) => (b.points < a.points ? b : a));

    let mostBench = flat.reduce((a, b) => (b.bench > a.bench ? b : a));

    // Biggest mover this GW
    const movers = managers
      .map((m) => ({ name: m.player_name, move: (m.last_rank || m.rank) - m.rank }))
      .filter((x) => Number.isFinite(x.move));
    const climber = movers.reduce((a, b) => (b.move > a.move ? b : a), { move: -Infinity });
    const faller = movers.reduce((a, b) => (b.move < a.move ? b : a), { move: Infinity });

    const tiles = [
      {
        label: 'Highest GW score',
        value: highest.points + ' pts',
        who: `${highest.name} · GW ${highest.gw}`,
      },
      {
        label: 'Lowest GW score',
        value: lowest.points + ' pts',
        who: `${lowest.name} · GW ${lowest.gw}`,
      },
      {
        label: 'Most points benched',
        value: mostBench.bench + ' pts',
        who: `${mostBench.name} · GW ${mostBench.gw}`,
      },
      {
        label: 'Climber of the week',
        value: climber.move > 0 ? '▲ ' + climber.move : '–',
        who: climber.move > 0 ? climber.name : 'No movement',
      },
    ];

    const host = document.getElementById('stats');
    tiles.forEach((t) => {
      const el = document.createElement('div');
      el.className = 'stat';
      el.innerHTML = `
        <div class="label">${t.label}</div>
        <div class="value">${t.value}</div>
        <div class="who">${escapeHtml(t.who)}</div>
      `;
      host.appendChild(el);
    });
  }
})();
</script>
</body>
</html>
"""


def render_html(dataset: dict) -> str:
    league_name = dataset["league"]["name"]
    league_id = dataset["league"]["id"]
    # Embed safely: escape </script> sequences inside the JSON payload.
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


def main() -> int:
    p = argparse.ArgumentParser(description="Build an FPL league dashboard.")
    p.add_argument("--league-id", type=int, required=True)
    p.add_argument("--output", type=Path, default=Path("index.html"))
    args = p.parse_args()

    dataset = build_dataset(args.league_id)
    html = render_html(dataset)
    args.output.write_text(html, encoding="utf-8")
    print(f"Wrote {args.output} ({len(html):,} bytes, {len(dataset['managers'])} managers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

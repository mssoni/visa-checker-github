#!/usr/bin/env python3
"""
Generates a live status dashboard (index.html) from the visa checker's state file.
Deployed to GitHub Pages so you can check status anytime via a URL.
Also handles 2-week log cleanup.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = "visa_checker_state.json"
OUTPUT_DIR = "status-page"
LOG_RETENTION_DAYS = 14  # Clear logs older than 2 weeks


def load_state():
    path = Path(STATE_FILE)
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def cleanup_old_logs(state):
    """Remove check_history entries older than LOG_RETENTION_DAYS."""
    stats = state.get("weekly_stats", {})
    history = stats.get("check_history", [])
    cutoff = datetime.now(IST) - timedelta(days=LOG_RETENTION_DAYS)

    original_count = len(history)
    cleaned = []
    for entry in history:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts >= cutoff:
                cleaned.append(entry)
        except (ValueError, KeyError):
            cleaned.append(entry)  # keep entries we can't parse

    stats["check_history"] = cleaned
    removed = original_count - len(cleaned)
    if removed > 0:
        print(f"🧹 Cleaned up {removed} log entries older than {LOG_RETENTION_DAYS} days")
    return state


def generate_status_html(state):
    """Generate a beautiful HTML status dashboard."""
    now_ist = datetime.now(IST).strftime("%b %d, %Y %I:%M %p IST")
    last_check = state.get("last_check", "Never")
    try:
        last_check_fmt = datetime.fromisoformat(last_check).astimezone(IST).strftime("%b %d, %Y %I:%M %p IST")
    except (ValueError, TypeError):
        last_check_fmt = "Never"

    stats = state.get("weekly_stats", {})
    checks = stats.get("checks_performed", 0)
    slots_found = stats.get("slots_found", 0)
    errors = stats.get("errors", 0)
    cities_checked = stats.get("cities_checked", {})
    history = stats.get("check_history", [])

    # Overall status
    if checks == 0:
        overall_status = "waiting"
        status_text = "Waiting for first run"
        status_color = "#f59e0b"
        status_bg = "#fffbeb"
    elif errors > checks * 0.5:
        overall_status = "degraded"
        status_text = "Degraded — high error rate"
        status_color = "#ef4444"
        status_bg = "#fef2f2"
    else:
        overall_status = "healthy"
        status_text = "Healthy — running normally"
        status_color = "#22c55e"
        status_bg = "#f0fdf4"

    # Per-city status cards
    city_cards = ""
    for city in ["Mumbai", "New Delhi", "Chennai", "Hyderabad", "Kolkata"]:
        cd = cities_checked.get(city, {})
        c_checks = cd.get("checked", 0)
        c_errors = cd.get("errors", 0)
        c_slots = cd.get("slots_found", 0)
        c_last = cd.get("last_status", "No data yet")

        if c_slots > 0:
            dot_color = "#22c55e"
            card_border = "#bbf7d0"
        elif c_errors > 0 and c_checks == c_errors:
            dot_color = "#ef4444"
            card_border = "#fecaca"
        else:
            dot_color = "#9ca3af"
            card_border = "#e5e7eb"

        city_cards += f"""
        <div class="city-card" style="border-color:{card_border}">
            <div class="city-header">
                <span class="dot" style="background:{dot_color}"></span>
                <strong>{city}</strong>
            </div>
            <div class="city-stats">
                <span>Checks: {c_checks}</span>
                <span>Errors: {c_errors}</span>
                <span>Slots: <strong style="color:{'#22c55e' if c_slots > 0 else '#6b7280'}">{c_slots}</strong></span>
            </div>
            <div class="city-last">Last: {c_last}</div>
        </div>"""

    # Recent history table rows (last 30 entries, newest first)
    recent = list(reversed(history[-30:]))
    history_rows = ""
    for entry in recent:
        try:
            ts = datetime.fromisoformat(entry["timestamp"]).astimezone(IST).strftime("%b %d %I:%M %p")
        except (ValueError, TypeError):
            ts = entry.get("timestamp", "?")
        s = entry.get("slots_found", 0)
        parsed = entry.get("total_parsed", 0)
        slot_badge = f'<span class="badge badge-green">{s} found</span>' if s > 0 else f'<span class="badge badge-gray">None</span>'
        history_rows += f"""
        <tr>
            <td>{ts}</td>
            <td>{parsed}</td>
            <td>{slot_badge}</td>
        </tr>"""

    if not history_rows:
        history_rows = '<tr><td colspan="3" style="text-align:center;color:#9ca3af;padding:24px">No checks recorded yet</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>US Visa Checker — Status Dashboard</title>
    <meta http-equiv="refresh" content="300">
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f9fafb; color:#1f2937; }}
        .container {{ max-width:800px; margin:0 auto; padding:24px 16px; }}
        h1 {{ font-size:24px; margin-bottom:4px; }}
        .subtitle {{ color:#6b7280; margin-bottom:24px; font-size:14px; }}

        .status-banner {{
            background:{status_bg}; border:1px solid {status_color}33;
            border-radius:12px; padding:16px 20px; margin-bottom:24px;
            display:flex; align-items:center; gap:12px;
        }}
        .status-dot {{ width:12px; height:12px; border-radius:50%; background:{status_color}; flex-shrink:0;
            animation: pulse 2s infinite;
        }}
        @keyframes pulse {{
            0%, 100% {{ opacity:1; }}
            50% {{ opacity:0.5; }}
        }}
        .status-text {{ font-weight:600; color:{status_color}; }}
        .status-sub {{ color:#6b7280; font-size:13px; }}

        .stats-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:24px; }}
        .stat-card {{ background:white; border:1px solid #e5e7eb; border-radius:10px; padding:16px; }}
        .stat-label {{ font-size:12px; color:#6b7280; text-transform:uppercase; letter-spacing:0.5px; }}
        .stat-value {{ font-size:28px; font-weight:700; margin-top:4px; }}

        .section-title {{ font-size:16px; font-weight:600; margin:24px 0 12px; }}

        .city-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-bottom:24px; }}
        .city-card {{ background:white; border:2px solid #e5e7eb; border-radius:10px; padding:14px; }}
        .city-header {{ display:flex; align-items:center; gap:8px; margin-bottom:8px; }}
        .dot {{ width:8px; height:8px; border-radius:50%; }}
        .city-stats {{ display:flex; gap:12px; font-size:13px; color:#6b7280; }}
        .city-last {{ font-size:12px; color:#9ca3af; margin-top:6px; }}

        table {{ width:100%; border-collapse:collapse; background:white; border-radius:10px; overflow:hidden; border:1px solid #e5e7eb; }}
        th {{ background:#f9fafb; padding:10px 14px; text-align:left; font-size:13px; color:#6b7280; border-bottom:1px solid #e5e7eb; }}
        td {{ padding:10px 14px; border-bottom:1px solid #f3f4f6; font-size:14px; }}

        .badge {{ padding:2px 8px; border-radius:12px; font-size:12px; font-weight:500; }}
        .badge-green {{ background:#dcfce7; color:#166534; }}
        .badge-gray {{ background:#f3f4f6; color:#6b7280; }}

        .footer {{ margin-top:32px; padding-top:16px; border-top:1px solid #e5e7eb; font-size:12px; color:#9ca3af; text-align:center; }}
        .footer a {{ color:#6366f1; text-decoration:none; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>US Visa Appointment Checker</h1>
        <p class="subtitle">Monitoring H &amp; L visas across 5 Indian cities &bull; Updated {now_ist}</p>

        <div class="status-banner">
            <div class="status-dot"></div>
            <div>
                <div class="status-text">{status_text}</div>
                <div class="status-sub">Last check: {last_check_fmt} &bull; Next: ~15 min</div>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Checks This Week</div>
                <div class="stat-value">{checks}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Slots Found</div>
                <div class="stat-value" style="color:{'#22c55e' if slots_found > 0 else '#ef4444'}">{slots_found}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Errors</div>
                <div class="stat-value" style="color:{'#ef4444' if errors > 0 else '#22c55e'}">{errors}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Log Entries</div>
                <div class="stat-value">{len(history)}</div>
            </div>
        </div>

        <div class="section-title">City Status</div>
        <div class="city-grid">{city_cards}</div>

        <div class="section-title">Recent Checks (last 30)</div>
        <table>
            <thead>
                <tr>
                    <th>Time (IST)</th>
                    <th>Entries Parsed</th>
                    <th>Slots</th>
                </tr>
            </thead>
            <tbody>{history_rows}</tbody>
        </table>

        <div class="footer">
            Auto-refreshes every 5 min &bull; Logs cleared every 14 days &bull;
            <a href="https://visagrader.com/us-visa-time-slots-availability/india-ind" target="_blank">Open VisaGrader ↗</a>
        </div>
    </div>
</body>
</html>"""
    return html


def main():
    state = load_state()

    # Clean up old logs (older than 2 weeks)
    state = cleanup_old_logs(state)
    save_state(state)

    # Generate the HTML dashboard
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    html = generate_status_html(state)
    output_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(output_path, "w") as f:
        f.write(html)
    print(f"✅ Status page generated: {output_path}")

    # Also write a status.json for programmatic access
    summary = {
        "last_check": state.get("last_check"),
        "last_updated_ist": datetime.now(IST).isoformat(),
        "checks_this_week": state.get("weekly_stats", {}).get("checks_performed", 0),
        "slots_found_this_week": state.get("weekly_stats", {}).get("slots_found", 0),
        "errors_this_week": state.get("weekly_stats", {}).get("errors", 0),
        "cities": state.get("weekly_stats", {}).get("cities_checked", {}),
        "log_entries": len(state.get("weekly_stats", {}).get("check_history", [])),
    }
    with open(os.path.join(OUTPUT_DIR, "status.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(f"✅ Status JSON generated: {OUTPUT_DIR}/status.json")


if __name__ == "__main__":
    main()

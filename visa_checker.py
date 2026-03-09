#!/usr/bin/env python3
"""
US Visa Appointment Checker for India
Monitors VisaGrader.com for H1B visa slot availability across all 5 Indian cities.
Sends email + desktop notifications when slots are found.
"""

import re
import json
import smtplib
import subprocess
import sys
import os
import logging
import time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from pathlib import Path
from playwright.sync_api import sync_playwright

# IST timezone (UTC+5:30) — used so the digest fires at the right time
# regardless of where the server is located
IST = timezone(timedelta(hours=5, minutes=30))

# ─── CONFIGURATION ──────────────────────────────────────────────────────────
# Edit these settings before first run

CONFIG = {
    # Email settings — reads from environment variables (set in GitHub Secrets)
    # Fallback values are for local testing only
    "email_enabled": True,
    "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
    "sender_email": os.environ.get("SENDER_EMAIL", "YOUR_EMAIL@gmail.com"),
    "sender_password": os.environ.get("SENDER_PASSWORD", "YOUR_APP_PASSWORD"),
    "recipient_email": os.environ.get("RECIPIENT_EMAIL", os.environ.get("SENDER_EMAIL", "YOUR_EMAIL@gmail.com")),

    # Desktop notification (disabled on server, works locally)
    "desktop_notify": os.environ.get("DESKTOP_NOTIFY", "false").lower() == "true",

    # Visa types to monitor (set to True for types you care about)
    "watch_visa_types": {
        "H": True,     # H1B, H4
        "L": True,     # L1, L2
        "F": False,    # F1 Student
        "B": False,    # B1/B2 Visitor
        "O": False,    # O1
        "ALL": False,  # Monitor everything
    },

    # How often to check (in minutes) — used by the scheduler
    "check_interval_minutes": 15,

    # Weekly digest settings
    "weekly_digest_enabled": True,
    "weekly_digest_day": "Monday",   # Day of week to send the digest
    "weekly_digest_hour": 12,         # Hour (24h format) — 12 = 12 PM IST (set your Mac to IST)

    # Log file location
    "log_file": "visa_checker.log",

    # State file to track what we've already notified about
    "state_file": "visa_checker_state.json",
}

# ─── Indian consulate cities and their VisaGrader URL codes ─────────────────
INDIA_CITIES = {
    "Mumbai": "mumbai-P47",
    "New Delhi": "new-delhi-P46",
    "Chennai": "chennai-P48",
    "Hyderabad": "hyderabad-P85",
    "Kolkata": "kolkata-P49",
}

BASE_URL = "https://visagrader.com/us-visa-time-slots-availability/india-ind"

# ─── LOGGING SETUP ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"]),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── PLAYWRIGHT BROWSER INSTANCE ────────────────────────────────────────────
# We reuse a single browser instance across all city checks for speed
_playwright = None
_browser = None


def get_browser():
    """Get or create a shared Playwright browser instance."""
    global _playwright, _browser
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        log.info("Browser launched (Playwright Chromium)")
    return _browser


def close_browser():
    """Clean up the browser instance."""
    global _playwright, _browser
    if _browser:
        _browser.close()
        _browser = None
    if _playwright:
        _playwright.stop()
        _playwright = None


def load_state():
    """Load previously seen slots to avoid duplicate notifications."""
    state_path = Path(CONFIG["state_file"])
    if state_path.exists():
        with open(state_path, "r") as f:
            return json.load(f)
    return {
        "last_check": None,
        "notified_slots": [],
        "weekly_stats": {
            "checks_performed": 0,
            "slots_found": 0,
            "errors": 0,
            "cities_checked": {},
            "week_start": datetime.now().isoformat(),
            "last_digest_sent": None,
            "check_history": [],     # list of {timestamp, slots_found, errors}
        },
    }


def save_state(state):
    """Save state to disk."""
    with open(CONFIG["state_file"], "w") as f:
        json.dump(state, f, indent=2, default=str)


def fetch_city_page(city_name, city_code):
    """Fetch the VisaGrader page using a real headless browser."""
    url = f"{BASE_URL}/{city_code}"
    log.info(f"Checking {city_name}: {url}")

    try:
        browser = get_browser()
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()

        # Navigate and wait for content to load
        page.goto(url, wait_until="networkidle", timeout=60000)
        # Extra wait for any JS-rendered content
        time.sleep(3)

        html = page.content()
        context.close()

        log.info(f"  Fetched {city_name} successfully ({len(html)} bytes)")
        return html
    except Exception as e:
        log.error(f"Failed to fetch {city_name}: {e}")
        return None


def parse_appointments(html, city_name):
    """
    Parse the VisaGrader page HTML to extract appointment availability.
    Returns a list of dicts: {city, visa_type, date, status}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # VisaGrader uses tables or card-based layouts to show availability.
    # We look for multiple patterns to be robust to layout changes.

    # Pattern 1: Look for table rows with visa type and date info
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            cell_texts = [c.get_text(strip=True) for c in cells]
            if len(cell_texts) >= 2:
                row_text = " ".join(cell_texts).lower()
                # Check if this row mentions a visa type we care about
                for vtype in ["H-1B", "H-4", "H1B", "H4", "L-1", "L-2", "L1", "L2",
                              "F-1", "F1", "B-1", "B-2", "B1", "B2", "O-1", "O1"]:
                    if vtype.lower() in row_text:
                        # Check if there's a date (not "not available" or "N/A")
                        has_date = bool(re.search(r'\d{1,2}[\s/\-]\w+[\s/\-]\d{2,4}', " ".join(cell_texts)))
                        not_available = any(
                            na in row_text
                            for na in ["not available", "n/a", "no appointments", "unavailable"]
                        )
                        results.append({
                            "city": city_name,
                            "visa_type": vtype.upper(),
                            "raw_text": " | ".join(cell_texts),
                            "has_date": has_date,
                            "available": has_date and not not_available,
                        })

    # Pattern 2: Look for any text blocks mentioning dates and visa types
    all_text = soup.get_text()
    # Find date patterns near visa type mentions
    date_pattern = r'(\d{1,2}[\s/\-](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*[\s/\-]\d{2,4})'
    visa_pattern = r'(H-?1B?|H-?4|L-?1|L-?2|F-?1|B-?1|B-?2|O-?1)'

    # Find sections/divs with visa info
    for div in soup.find_all(["div", "section", "article", "p", "span", "li"]):
        text = div.get_text(strip=True)
        if len(text) > 10 and len(text) < 500:
            visa_matches = re.findall(visa_pattern, text, re.IGNORECASE)
            date_matches = re.findall(date_pattern, text, re.IGNORECASE)
            if visa_matches:
                not_available = any(
                    na in text.lower()
                    for na in ["not available", "n/a", "no appointments", "unavailable", "no slots"]
                )
                for vtype in set(visa_matches):
                    entry = {
                        "city": city_name,
                        "visa_type": vtype.upper().replace("-", ""),
                        "raw_text": text[:200],
                        "has_date": bool(date_matches),
                        "dates_found": date_matches if date_matches else [],
                        "available": bool(date_matches) and not not_available,
                    }
                    # Avoid duplicates
                    if entry not in results:
                        results.append(entry)

    # Pattern 3: Look for any "Available" or date badges/indicators
    for elem in soup.find_all(class_=re.compile(r'available|slot|appointment|date|badge', re.I)):
        text = elem.get_text(strip=True)
        if text and "not" not in text.lower():
            results.append({
                "city": city_name,
                "visa_type": "UNKNOWN",
                "raw_text": text[:200],
                "has_date": bool(re.search(date_pattern, text, re.I)),
                "available": True,
                "source": "css_class_match",
            })

    return results


def filter_watched_types(appointments):
    """Filter appointments to only watched visa types."""
    watched = CONFIG["watch_visa_types"]
    if watched.get("ALL"):
        return [a for a in appointments if a.get("available")]

    filtered = []
    for apt in appointments:
        if not apt.get("available"):
            continue
        vtype = apt["visa_type"].upper()
        # Map to category
        category = vtype[0] if vtype else ""
        if watched.get(category, False):
            filtered.append(apt)

    return filtered


def send_desktop_notification(title, message):
    """Send a macOS desktop notification."""
    if not CONFIG["desktop_notify"]:
        return
    try:
        # macOS
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "{title}" sound name "Glass"'
        ], check=True, timeout=10)
        log.info("Desktop notification sent")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        # Try Linux notify-send as fallback
        try:
            subprocess.run(["notify-send", title, message], check=True, timeout=10)
            log.info("Desktop notification sent (Linux)")
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            log.warning("Could not send desktop notification")


def send_email_notification(subject, body_html):
    """Send an email notification."""
    if not CONFIG["email_enabled"]:
        return

    if "YOUR_EMAIL" in CONFIG["sender_email"] or "YOUR_APP_PASSWORD" in CONFIG["sender_password"]:
        log.warning("Email not configured — skipping email notification. Edit CONFIG to set up email.")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = CONFIG["sender_email"]
        msg["To"] = CONFIG["recipient_email"]

        # Plain text version
        plain = BeautifulSoup(body_html, "html.parser").get_text()
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(CONFIG["smtp_server"], CONFIG["smtp_port"]) as server:
            server.starttls()
            server.login(CONFIG["sender_email"], CONFIG["sender_password"])
            server.sendmail(CONFIG["sender_email"], CONFIG["recipient_email"], msg.as_string())

        log.info(f"Email notification sent to {CONFIG['recipient_email']}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def build_notification_html(available_slots):
    """Build a nicely formatted HTML email body."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = ""
    for slot in available_slots:
        dates = ", ".join(slot.get("dates_found", [])) or "See website"
        rows += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{slot['city']}</td>
            <td style="padding:8px;border:1px solid #ddd">{slot['visa_type']}</td>
            <td style="padding:8px;border:1px solid #ddd">{dates}</td>
            <td style="padding:8px;border:1px solid #ddd;font-size:12px;color:#666">{slot['raw_text'][:100]}</td>
        </tr>"""

    return f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
        <h2 style="color:#2563eb">🎯 US Visa Slots Available in India!</h2>
        <p>Checked at: {now}</p>
        <table style="border-collapse:collapse;width:100%">
            <tr style="background:#2563eb;color:white">
                <th style="padding:8px;border:1px solid #ddd">City</th>
                <th style="padding:8px;border:1px solid #ddd">Visa Type</th>
                <th style="padding:8px;border:1px solid #ddd">Dates</th>
                <th style="padding:8px;border:1px solid #ddd">Details</th>
            </tr>
            {rows}
        </table>
        <p style="margin-top:16px">
            <a href="https://visagrader.com/us-visa-time-slots-availability/india-ind"
               style="background:#2563eb;color:white;padding:10px 20px;text-decoration:none;border-radius:4px">
                Book Now on VisaGrader →
            </a>
        </p>
        <p style="color:#666;font-size:12px;margin-top:24px">
            This alert was sent by your Visa Appointment Checker script.
        </p>
    </body>
    </html>
    """


def now_ist():
    """Get current time in IST, regardless of server timezone."""
    return datetime.now(IST)


def should_send_weekly_digest(state):
    """Check if it's time to send the weekly digest (uses IST)."""
    if not CONFIG["weekly_digest_enabled"]:
        return False

    now = now_ist()
    target_day = CONFIG["weekly_digest_day"]
    target_hour = CONFIG["weekly_digest_hour"]

    # Check if today is the right day and hour in IST
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if days[now.weekday()] != target_day:
        return False
    if now.hour != target_hour:
        return False

    # Check we haven't already sent one today
    last_sent = state.get("weekly_stats", {}).get("last_digest_sent")
    if last_sent:
        try:
            last_dt = datetime.fromisoformat(last_sent)
            if last_dt.astimezone(IST).date() == now.date():
                return False  # Already sent today
        except (ValueError, TypeError):
            pass

    return True


def build_weekly_digest_html(state):
    """Build a weekly summary email showing script health and check results."""
    stats = state.get("weekly_stats", {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    week_start = stats.get("week_start", "Unknown")
    try:
        week_start_fmt = datetime.fromisoformat(week_start).strftime("%b %d, %Y")
    except (ValueError, TypeError):
        week_start_fmt = week_start

    checks = stats.get("checks_performed", 0)
    slots_found = stats.get("slots_found", 0)
    errors = stats.get("errors", 0)
    cities_checked = stats.get("cities_checked", {})

    # Success rate
    total_city_checks = sum(cities_checked.get(c, {}).get("checked", 0) for c in cities_checked)
    total_city_errors = sum(cities_checked.get(c, {}).get("errors", 0) for c in cities_checked)
    success_rate = ((total_city_checks - total_city_errors) / max(total_city_checks, 1)) * 100

    # Per-city rows
    city_rows = ""
    for city in ["Mumbai", "New Delhi", "Chennai", "Hyderabad", "Kolkata"]:
        city_data = cities_checked.get(city, {})
        c_checks = city_data.get("checked", 0)
        c_errors = city_data.get("errors", 0)
        c_slots = city_data.get("slots_found", 0)
        c_last = city_data.get("last_status", "No data")

        status_color = "#22c55e" if c_slots > 0 else "#ef4444"
        status_icon = "&#9679;" if c_slots > 0 else "&#9679;"
        status_text = f"{c_slots} slot(s) detected" if c_slots > 0 else "No slots"

        city_rows += f"""
        <tr>
            <td style="padding:8px;border:1px solid #e5e7eb">{city}</td>
            <td style="padding:8px;border:1px solid #e5e7eb">{c_checks}</td>
            <td style="padding:8px;border:1px solid #e5e7eb">{c_errors}</td>
            <td style="padding:8px;border:1px solid #e5e7eb">
                <span style="color:{status_color}">{status_icon}</span> {status_text}
            </td>
        </tr>"""

    # Check frequency info
    interval = CONFIG["check_interval_minutes"]
    expected_checks = (7 * 24 * 60) // interval  # expected per week

    watched_types = [k for k, v in CONFIG["watch_visa_types"].items() if v]

    return f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;color:#1f2937">
        <h2 style="color:#6366f1">📊 Weekly Visa Checker Digest</h2>
        <p style="color:#6b7280">Week of {week_start_fmt} &mdash; Generated {now}</p>

        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;margin:16px 0">
            <h3 style="margin:0 0 8px 0;color:#166534">✅ Script is running</h3>
            <p style="margin:0;color:#15803d">
                {checks} checks completed this week (every {interval} min, ~{expected_checks} expected)
            </p>
        </div>

        <h3 style="color:#374151">Overview</h3>
        <table style="border-collapse:collapse;width:100%;margin:8px 0">
            <tr>
                <td style="padding:12px;background:#f9fafb;border:1px solid #e5e7eb;width:50%">
                    <strong>Total Checks</strong><br>{checks}
                </td>
                <td style="padding:12px;background:#f9fafb;border:1px solid #e5e7eb;width:50%">
                    <strong>Fetch Errors</strong><br>{errors}
                </td>
            </tr>
            <tr>
                <td style="padding:12px;background:#f9fafb;border:1px solid #e5e7eb">
                    <strong>Slots Found</strong><br>
                    <span style="color:{'#22c55e' if slots_found > 0 else '#ef4444'};font-size:18px;font-weight:bold">
                        {slots_found}
                    </span>
                </td>
                <td style="padding:12px;background:#f9fafb;border:1px solid #e5e7eb">
                    <strong>Success Rate</strong><br>{success_rate:.0f}%
                </td>
            </tr>
        </table>

        <h3 style="color:#374151">Per-City Breakdown</h3>
        <table style="border-collapse:collapse;width:100%">
            <tr style="background:#6366f1;color:white">
                <th style="padding:8px;border:1px solid #e5e7eb;text-align:left">City</th>
                <th style="padding:8px;border:1px solid #e5e7eb;text-align:left">Checks</th>
                <th style="padding:8px;border:1px solid #e5e7eb;text-align:left">Errors</th>
                <th style="padding:8px;border:1px solid #e5e7eb;text-align:left">Status</th>
            </tr>
            {city_rows}
        </table>

        <h3 style="color:#374151">Configuration</h3>
        <ul style="color:#6b7280">
            <li>Monitoring visa types: <strong>{', '.join(watched_types)}</strong></li>
            <li>Check interval: <strong>every {interval} minutes</strong></li>
            <li>Cities: <strong>all 5 Indian consulates</strong></li>
        </ul>

        <p style="color:#9ca3af;font-size:12px;margin-top:24px;border-top:1px solid #e5e7eb;padding-top:12px">
            This is your weekly automated digest from the US Visa Appointment Checker.
            Next digest: next {CONFIG['weekly_digest_day']} at {CONFIG['weekly_digest_hour']}:00.
            <br>To stop these, set <code>weekly_digest_enabled</code> to <code>False</code> in the script config.
        </p>
    </body>
    </html>
    """


def send_weekly_digest(state):
    """Send the weekly digest and reset weekly counters."""
    log.info("📊 Sending weekly digest email...")

    html = build_weekly_digest_html(state)
    stats = state.get("weekly_stats", {})
    slots = stats.get("slots_found", 0)

    subject = f"📊 Visa Checker Weekly Digest — {'🎯 ' + str(slots) + ' slot(s) found!' if slots > 0 else 'No slots this week'}"

    send_email_notification(subject, html)
    send_desktop_notification(
        "Visa Checker Weekly Digest",
        f"Week summary: {stats.get('checks_performed', 0)} checks, {slots} slots found"
    )

    # Reset weekly stats
    state["weekly_stats"] = {
        "checks_performed": 0,
        "slots_found": 0,
        "errors": 0,
        "cities_checked": {},
        "week_start": datetime.now().isoformat(),
        "last_digest_sent": datetime.now().isoformat(),
        "check_history": [],
    }
    save_state(state)
    log.info("Weekly digest sent and stats reset.")


def update_weekly_stats(state, city_name, had_error=False, slots_found=0):
    """Update the running weekly statistics for a city check."""
    stats = state.setdefault("weekly_stats", {
        "checks_performed": 0, "slots_found": 0, "errors": 0,
        "cities_checked": {}, "week_start": datetime.now().isoformat(),
        "last_digest_sent": None, "check_history": [],
    })

    if city_name not in stats.setdefault("cities_checked", {}):
        stats["cities_checked"][city_name] = {"checked": 0, "errors": 0, "slots_found": 0, "last_status": ""}

    city_stats = stats["cities_checked"][city_name]
    city_stats["checked"] += 1
    if had_error:
        city_stats["errors"] += 1
        stats["errors"] = stats.get("errors", 0) + 1
        city_stats["last_status"] = "Fetch error"
    else:
        city_stats["slots_found"] += slots_found
        city_stats["last_status"] = f"{slots_found} slot(s)" if slots_found > 0 else "No slots"

    stats["slots_found"] = stats.get("slots_found", 0) + slots_found


def check_all_cities():
    """Main function: check all Indian cities and notify if slots found."""
    log.info("=" * 60)
    log.info(f"Starting visa appointment check at {datetime.now()}")
    log.info("=" * 60)

    state = load_state()
    all_appointments = []
    available_slots = []

    for city_name, city_code in INDIA_CITIES.items():
        html = fetch_city_page(city_name, city_code)
        if not html:
            update_weekly_stats(state, city_name, had_error=True)
            continue

        appointments = parse_appointments(html, city_name)
        all_appointments.extend(appointments)

        # Log what we found
        available = [a for a in appointments if a.get("available")]
        unavailable = [a for a in appointments if not a.get("available")]

        # Track weekly stats per city
        update_weekly_stats(state, city_name, had_error=False, slots_found=len(available))

        if available:
            log.info(f"  ✅ {city_name}: {len(available)} slot(s) AVAILABLE!")
            for a in available:
                log.info(f"     → {a['visa_type']}: {a['raw_text'][:80]}")
        else:
            log.info(f"  ❌ {city_name}: No available slots detected")
            for a in unavailable[:3]:  # Show first few entries
                log.info(f"     → {a['visa_type']}: {a['raw_text'][:80]}")

    # Filter to only watched visa types
    available_slots = filter_watched_types(all_appointments)

    # Check against previous state to avoid duplicate notifications
    new_slots = []
    notified_keys = set(state.get("notified_slots", []))
    for slot in available_slots:
        key = f"{slot['city']}_{slot['visa_type']}_{slot.get('dates_found', '')}"
        if key not in notified_keys:
            new_slots.append(slot)
            notified_keys.add(key)

    # Send notifications for new slots
    if new_slots:
        log.info(f"\n🎉 {len(new_slots)} NEW slot(s) found! Sending notifications...")

        # Desktop notification
        cities_with_slots = set(s["city"] for s in new_slots)
        types_with_slots = set(s["visa_type"] for s in new_slots)
        desktop_msg = f"Slots in: {', '.join(cities_with_slots)} for {', '.join(types_with_slots)}"
        send_desktop_notification("US Visa Slot Available! 🇺🇸", desktop_msg)

        # Email notification
        email_html = build_notification_html(new_slots)
        send_email_notification(
            f"🇺🇸 US Visa Slots Available in India! ({', '.join(cities_with_slots)})",
            email_html,
        )
    else:
        log.info("\nNo new slots found. Will check again later.")

    # Update state
    state["last_check"] = datetime.now().isoformat()
    state["notified_slots"] = list(notified_keys)

    # Increment weekly check counter
    stats = state.setdefault("weekly_stats", {})
    stats["checks_performed"] = stats.get("checks_performed", 0) + 1
    stats.setdefault("check_history", []).append({
        "timestamp": datetime.now().isoformat(),
        "slots_found": len(available_slots),
        "total_parsed": len(all_appointments),
    })
    # Keep only last 500 history entries to avoid file bloat
    if len(stats["check_history"]) > 500:
        stats["check_history"] = stats["check_history"][-500:]

    save_state(state)

    # Check if it's time to send the weekly digest
    if should_send_weekly_digest(state):
        send_weekly_digest(state)

    # Summary
    log.info(f"\n{'=' * 60}")
    log.info(f"Check complete. Total entries parsed: {len(all_appointments)}")
    log.info(f"Available slots (watched types): {len(available_slots)}")
    log.info(f"New slots (not previously notified): {len(new_slots)}")
    log.info(f"{'=' * 60}\n")

    return {
        "total_parsed": len(all_appointments),
        "available": len(available_slots),
        "new_notifications": len(new_slots),
        "slots": available_slots,
    }


# ─── Also scrape the main India page for a summary view ────────────────────
def check_main_page():
    """Check the main India overview page for a quick summary."""
    log.info("Checking main India overview page...")
    url = BASE_URL
    try:
        html = fetch_city_page("India Overview", "")
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        relevant = []
        for line in text.split("\n"):
            line_lower = line.lower()
            if any(city.lower() in line_lower for city in INDIA_CITIES.keys()):
                relevant.append(line.strip())

        if relevant:
            log.info("Main page summary:")
            for line in relevant[:20]:
                log.info(f"  {line}")
        return relevant
    except Exception as e:
        log.error(f"Failed to fetch main page: {e}")
        return []


if __name__ == "__main__":
    log.info("US Visa Appointment Checker for India")
    log.info(f"Monitoring cities: {', '.join(INDIA_CITIES.keys())}")
    log.info(f"Watching visa types: {[k for k, v in CONFIG['watch_visa_types'].items() if v]}")
    log.info("")

    try:
        # Check main overview page first
        check_main_page()
        log.info("")

        # Check each city
        result = check_all_cities()

        print(f"\n{'─' * 40}")
        print(f"Results: {result['available']} available slot(s) found")
        print(f"New notifications sent: {result['new_notifications']}")
        if result['slots']:
            print("\nAvailable slots:")
            for s in result['slots']:
                print(f"  • {s['city']} — {s['visa_type']}: {s['raw_text'][:80]}")
        print(f"{'─' * 40}")
    finally:
        # Always clean up the browser
        close_browser()
        log.info("Browser closed.")

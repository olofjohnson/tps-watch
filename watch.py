#!/usr/bin/env python3
"""
TPS Watch — push alerts for Ukraine-related DHS/USCIS documents.

Checks, every run:
  1. Federal Register PUBLIC INSPECTION  (earliest official signal, usually
     a business day or more before publication)
  2. Federal Register PUBLISHED documents since LOOKBACK_START
  3. USCIS TPS Ukraine page   — alerts once if "April 19, 2027" appears
  4. USCIS I-9 Central news   — alerts on any new Ukraine-related item
     (3 and 4 are best-effort: USCIS sometimes blocks automated requests)

Standard library only. Configuration via environment variables:
  NTFY_TOPIC      required — ntfy topic name (store as a GitHub secret)
  NTFY_SERVER     optional — default https://ntfy.sh
  LOOKBACK_START  optional — earliest publication date, default 2026-08-01
  TEST_MODE       optional — "true" sends a status summary notification
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

STATE_FILE = "seen.json"
FR_API = "https://www.federalregister.gov/api/v1"
USCIS_TPS_PAGE = "https://www.uscis.gov/humanitarian/temporary-protected-status/TPS-Ukraine"
USCIS_I9_NEWS = "https://www.uscis.gov/i-9-central/form-i-9-related-news"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
LOOKBACK_START = os.environ.get("LOOKBACK_START", "2026-08-01")
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() == "true"

API_UA = "tps-watch/1.0 (personal monitoring script)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DHS_MARKERS = ("homeland security", "citizenship and immigration")
MAX_PAGES = 15


# ----------------------------------------------------------------- utilities
def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def http_get(url, browser=False, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA if browser else API_UA,
        "Accept": "text/html,application/xhtml+xml" if browser else "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("fr_seen", [])
    state.setdefault("i9_seen", [])
    state.setdefault("i9_initialized", False)
    state.setdefault("tps_2027_alerted", False)
    state.setdefault("last_heartbeat_week", "")
    return state


def save_state(state):
    state["fr_seen"] = sorted(set(state["fr_seen"]))
    state["i9_seen"] = sorted(set(state["i9_seen"]))
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def notify(title, message, priority=4, click=None, tags=None):
    """Publish to ntfy using the JSON API (avoids header-encoding problems)."""
    if not NTFY_TOPIC:
        log("NTFY_TOPIC is not set — cannot send notification.")
        return False
    payload = {"topic": NTFY_TOPIC, "title": title[:250], "message": message[:3500],
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        NTFY_SERVER + "/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": API_UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
    except urllib.error.URLError as e:
        log(f"ntfy publish failed: {e}")
        return False
    log(f"Notification sent: {title}" if ok else "ntfy returned a non-2xx status")
    return ok


# ---------------------------------------------------------------- matching
def agency_text(doc):
    names = []
    for a in doc.get("agencies") or []:
        if isinstance(a, dict):
            names += [a.get("name") or "", a.get("raw_name") or "", a.get("slug") or ""]
    names += doc.get("agency_names") or []
    return " ".join(n for n in names if n).lower()


def doc_text(doc):
    parts = [doc.get("title"), doc.get("toc_subject"), doc.get("toc_doc")]
    return " ".join(p for p in parts if p)


def is_relevant(doc):
    """Ukraine in the title, and issued by DHS/USCIS (or agency unknown)."""
    if "ukraine" not in doc_text(doc).lower():
        return False
    agencies = agency_text(doc)
    return (not agencies) or any(m in agencies for m in DHS_MARKERS)


def is_tps(doc):
    t = doc_text(doc).lower()
    return "temporary protected status" in t or re.search(r"\btps\b", t) is not None


def classify(doc):
    t = doc_text(doc).lower()
    if is_tps(doc):
        if "terminat" in t:
            return "TERMINATION", 5, ["rotating_light"]
        if "extension" in t or "extend" in t or "redesignat" in t:
            return "EXTENSION", 5, ["tada"]
        return "TPS NOTICE", 5, ["warning"]
    return "Ukraine / DHS notice", 4, ["memo"]


# ------------------------------------------------------- Federal Register
def fr_public_inspection():
    data = json.loads(http_get(f"{FR_API}/public-inspection-documents/current.json"))
    return data.get("results") or []


def fr_published():
    params = [("conditions[term]", "Ukraine"),
              ("conditions[publication_date][gte]", LOOKBACK_START),
              ("order", "newest"), ("per_page", "100")]
    url = f"{FR_API}/documents.json?{urllib.parse.urlencode(params)}"
    results, pages = [], 0
    while url and pages < MAX_PAGES:
        data = json.loads(http_get(url))
        results += data.get("results") or []
        url = data.get("next_page_url")
        pages += 1
    return results


def process_fr(docs, source, state, new_alerts):
    seen = set(state["fr_seen"])
    matched = 0
    for doc in docs:
        num = doc.get("document_number")
        if not num or not is_relevant(doc):
            continue
        matched += 1
        if num in seen:
            continue
        seen.add(num)
        label, prio, tags = classify(doc)
        when = doc.get("publication_date") or "date not listed"
        title = doc_text(doc) or "(untitled)"
        new_alerts.append({
            "title": f"{label}: Federal Register ({source})",
            "message": f"{title}\n\nPublication date: {when}\nDocument: {num}",
            "priority": prio, "tags": tags,
            "click": doc.get("html_url") or doc.get("pdf_url"),
        })
    state["fr_seen"] = list(seen)
    return matched


# ------------------------------------------------------------------ USCIS
def check_tps_page(state, new_alerts):
    html = http_get(USCIS_TPS_PAGE, browser=True)
    found = re.search(r"(April|Apr\.?)\s+19,\s+2027", html) is not None
    if found and not state["tps_2027_alerted"]:
        state["tps_2027_alerted"] = True
        new_alerts.append({
            "title": "USCIS TPS Ukraine page now mentions April 19, 2027",
            "message": "The USCIS TPS Ukraine page has changed and now references "
                       "April 19, 2027 — possibly acknowledging the automatic extension. "
                       "Open the page to confirm.",
            "priority": 5, "tags": ["tada"], "click": USCIS_TPS_PAGE})
    return found


def check_i9_news(state, new_alerts):
    html = http_get(USCIS_I9_NEWS, browser=True)
    links = set(re.findall(r'href="(/i-9-central/form-i-9-related-news/[^"#?]+)"', html))
    ukraine = {l for l in links if "ukraine" in l.lower()}
    if not state["i9_initialized"]:
        # First successful read: record what already exists without alerting.
        state["i9_seen"] = sorted(ukraine)
        state["i9_initialized"] = True
        return ukraine, True
    seen = set(state["i9_seen"])
    for link in sorted(ukraine - seen):
        new_alerts.append({
            "title": "New USCIS I-9 Central item about Ukraine",
            "message": f"A new Ukraine-related item appeared on USCIS I-9 Central:\n{link}",
            "priority": 5, "tags": ["warning"], "click": "https://www.uscis.gov" + link})
    state["i9_seen"] = sorted(seen | ukraine)
    return ukraine, False


# ------------------------------------------------------------------- main
def main():
    if not NTFY_TOPIC:
        log("ERROR: NTFY_TOPIC secret is missing. Add it under Settings > Secrets "
            "and variables > Actions.")
        return 1

    state = load_state()
    alerts, status, fr_failures = [], [], 0

    for source, fetch in (("public inspection", fr_public_inspection),
                          ("published", fr_published)):
        try:
            docs = fetch()
            matched = process_fr(docs, source, state, alerts)
            status.append(f"FR {source}: {len(docs)} scanned, {matched} Ukraine/DHS match(es)")
            log(status[-1])
        except Exception as e:  # noqa: BLE001 — report any failure, keep going
            fr_failures += 1
            status.append(f"FR {source}: FAILED ({e})")
            log(status[-1])

    try:
        found = check_tps_page(state, alerts)
        status.append(f"USCIS TPS page: read OK, 'April 19, 2027' {'FOUND' if found else 'not present'}")
    except Exception as e:  # noqa: BLE001
        status.append(f"USCIS TPS page: could not read ({e})")
    log(status[-1])

    try:
        items, first = check_i9_news(state, alerts)
        note = " (baseline recorded)" if first else ""
        status.append(f"USCIS I-9 news: read OK, {len(items)} Ukraine item(s){note}")
    except Exception as e:  # noqa: BLE001
        status.append(f"USCIS I-9 news: could not read ({e})")
    log(status[-1])

    for a in alerts:
        notify(a["title"], a["message"], a["priority"], a.get("click"), a.get("tags"))
    if not alerts:
        log("No new items.")

    # Weekly heartbeat (Mondays) — proves the watcher is alive, and its state
    # commit keeps the repository active so GitHub doesn't disable the schedule.
    now = datetime.now(timezone.utc)
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if now.weekday() == 0 and state["last_heartbeat_week"] != week:
        state["last_heartbeat_week"] = week
        notify("TPS watch: weekly check-in",
               "Still running. Nothing new since the last alert.\n\n" + "\n".join(status),
               priority=2, tags=["white_check_mark"])

    if TEST_MODE:
        notify("TPS watch: test OK",
               f"Pipeline working. {len(alerts)} new alert(s) this run.\n\n"
               + "\n".join(status) + f"\n\nLookback start: {LOOKBACK_START}",
               priority=3, tags=["white_check_mark"])

    save_state(state)
    # Fail the run only if BOTH Federal Register checks failed, so GitHub emails you.
    return 1 if fr_failures == 2 else 0


if __name__ == "__main__":
    sys.exit(main())

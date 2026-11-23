#!/usr/bin/env python3
"""
Internship alert bot: finance x sport / running / private wealth.
Checks company job boards + the SimplifyJobs Summer 2027 list, filters, and
emails you only NEW matches. Requires only `requests` (pip install requests).

Env vars (set as GitHub Actions secrets):
  EMAIL_FROM      your Gmail address
  EMAIL_APP_PASS  a Gmail "App Password" (Google Account > Security > App passwords)
  EMAIL_TO        where to send alerts (can be the same address)
  DISCORD_WEBHOOK optional, also posts to a Discord channel
"""
import json, os, re, smtplib, sys
from email.message import EmailMessage
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIG: edit this section
# ----------------------------------------------------------------------------

# Job must contain one of these in the title
INTERN_WORDS = ["intern", "internship", "co-op", "coop"]

# For companies marked topic_filter=True, the title must ALSO match one of these
TOPIC_WORDS = [
    "financ", "wealth", "analyst", "accounting", "strategy", "business",
    "operations", "investment", "planning", "advisor", "partnership",
    "sports", "athlete", "fp&a", "treasury", "corporate development",
]

# Location must contain one of these (jobs with a blank location are kept)
LOCATIONS = [
    "boston", "cambridge", "massachusetts", ", ma", "waltham",
    "san francisco", "bay area", "palo alto", "san jose", "oakland", "california", ", ca",
    "portland", "beaverton", "oregon", ", or", "eugene", "bend",
    "remote",
]

# Company boards. ats = greenhouse | lever | ashby
# token = the company's slug on its job board URL, e.g.
#   boards.greenhouse.io/<token>   jobs.lever.co/<token>   jobs.ashbyhq.com/<token>
# topic_filter=False means "show me ANY internship" (good for small companies and
# wealth firms). A wrong token just prints a warning and is skipped, so add freely.
COMPANIES = [
    {"name": "Strava",       "ats": "greenhouse", "token": "strava",       "topic_filter": True},
    {"name": "WHOOP",        "ats": "greenhouse", "token": "whoop",        "topic_filter": True},
    {"name": "Tracksmith",   "ats": "lever",      "token": "tracksmith",   "topic_filter": False},
    {"name": "Oiselle",      "ats": "lever",      "token": "oiselle",      "topic_filter": False},
    {"name": "On Running",   "ats": "greenhouse", "token": "onrunning",    "topic_filter": True},
    {"name": "Columbia",     "ats": "greenhouse", "token": "columbiasportswear", "topic_filter": True},
    # Add more: {"name": "...", "ats": "...", "token": "...", "topic_filter": False},
]

# Community-maintained list (fast for big firms). Set to None to disable.
# Unverified path: open the repo in your browser and confirm the file exists.
SIMPLIFY_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)
# Only keep Simplify listings whose company OR title matches one of these
SIMPLIFY_KEYWORDS = [
    "wealth", "private", "financ", "invest", "sport", "nike", "adidas", "new balance",
    "fidelity", "wellington", "state street", "john hancock", "morgan stanley",
    "merrill", "goldman", "schwab", "wasserman", "puma", "under armour",
]

MAX_PER_EMAIL = 60
SEEN_FILE = Path(__file__).with_name("seen_jobs.json")
HEADERS = {"User-Agent": "internship-alert-bot/1.0"}

# ----------------------------------------------------------------------------
# Fetchers: each returns a list of dicts: id, company, title, location, url
# ----------------------------------------------------------------------------

def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_greenhouse(c):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs")
    return [{
        "id": f"gh-{c['token']}-{j['id']}", "company": c["name"], "title": j["title"],
        "location": (j.get("location") or {}).get("name", ""), "url": j["absolute_url"],
    } for j in data.get("jobs", [])]

def fetch_lever(c):
    data = get_json(f"https://api.lever.co/v0/postings/{c['token']}?mode=json")
    return [{
        "id": f"lv-{c['token']}-{j['id']}", "company": c["name"], "title": j["text"],
        "location": (j.get("categories") or {}).get("location", "") or "", "url": j["hostedUrl"],
    } for j in data]

def fetch_ashby(c):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}")
    return [{
        "id": f"ab-{c['token']}-{j['id']}", "company": c["name"], "title": j["title"],
        "location": j.get("location", "") or "", "url": j.get("jobUrl", ""),
    } for j in data.get("jobs", [])]

FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}

def fetch_simplify():
    if not SIMPLIFY_URL:
        return []
    data = get_json(SIMPLIFY_URL)
    out = []
    for j in data:
        if not j.get("active", True) or not j.get("is_visible", True):
            continue
        text = f"{j.get('company_name','')} {j.get('title','')}".lower()
        if not any(k in text for k in SIMPLIFY_KEYWORDS):
            continue
        out.append({
            "id": f"sm-{j.get('id') or j.get('url')}", "company": j.get("company_name", ""),
            "title": j.get("title", ""), "location": "; ".join(j.get("locations", [])),
            "url": j.get("url", ""), "simplify": True,
        })
    return out

# ----------------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------------

def is_match(job, topic_filter):
    title = job["title"].lower()
    if not any(w in title for w in INTERN_WORDS):
        return False
    # Skip stale postings for earlier years unless they also mention 2027
    if re.search(r"20(24|25|26)", title) and "2027" not in title:
        return False
    if topic_filter and not any(w in title for w in TOPIC_WORDS):
        return False
    loc = job["location"].lower()
    if loc and not any(l in loc for l in LOCATIONS):
        return False
    return True

# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------

def build_message(jobs):
    lines = []
    for j in jobs[:MAX_PER_EMAIL]:
        lines.append(f"{j['company']}: {j['title']}\n  {j['location'] or 'location not listed'}\n  {j['url']}\n")
    if len(jobs) > MAX_PER_EMAIL:
        lines.append(f"...and {len(jobs) - MAX_PER_EMAIL} more.")
    return "\n".join(lines)

def send_email(subject, body):
    sender, pw, to = (os.environ.get(k) for k in ("EMAIL_FROM", "EMAIL_APP_PASS", "EMAIL_TO"))
    if not (sender and pw and to):
        print("Email not configured; skipping email.")
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, pw)
        s.send_message(msg)

def send_discord(body):
    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        requests.post(hook, json={"content": body[:1900]}, timeout=20)

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    matches = []

    for c in COMPANIES:
        try:
            jobs = FETCHERS[c["ats"]](c)
        except Exception as e:
            print(f"WARNING: could not read {c['name']} ({c['ats']}/{c['token']}): {e}")
            continue
        matches += [j for j in jobs if is_match(j, c["topic_filter"])]

    try:
        matches += [j for j in fetch_simplify() if is_match(j, topic_filter=False)]
    except Exception as e:
        print(f"WARNING: could not read Simplify list: {e}")

    new = [j for j in matches if j["id"] not in seen]
    print(f"{len(matches)} matches, {len(new)} new.")

    if new:
        body = build_message(new)
        send_email(f"{len(new)} new 2027 internship match(es)", body)
        send_discord(f"**{len(new)} new internship match(es)**\n{body}")

    seen.update(j["id"] for j in matches)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=1))

if __name__ == "__main__":
    sys.exit(main())

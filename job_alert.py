"""
Instant Job Alert Bot
---------------------
Checks job sources every few minutes and sends a Telegram message to your phone
the moment a NEW matching opening appears (with company, location and apply link).

Sources:
  1. Adzuna job API (aggregates many Indian job sites, supports sort-by-newest)
  2. Company career boards that use Greenhouse or Lever (public JSON feeds)

Only the Python standard library is needed.

Run once:            python job_alert.py
Run every 10 min:    python job_alert.py --loop 600
Environment variables (see README): ADZUNA_APP_ID, ADZUNA_APP_KEY,
                                    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
SEEN_FILE = os.path.join(HERE, "seen.json")


def http_json(url, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "job-alert-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- sources
def fetch_adzuna(cfg):
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("! ADZUNA_APP_ID / ADZUNA_APP_KEY not set, skipping Adzuna")
        return []
    jobs = []
    for s in cfg.get("searches", []):
        params = {
            "app_id": app_id, "app_key": app_key, "results_per_page": 50,
            "what": s["what"], "sort_by": "date",
            "max_days_old": cfg.get("max_days_old", 2),
            "content-type": "application/json",
        }
        if s.get("where"):
            params["where"] = s["where"]
        url = "https://api.adzuna.com/v1/api/jobs/in/search/1?" + urllib.parse.urlencode(params)
        try:
            for j in http_json(url).get("results", []):
                jobs.append({
                    "id": "adzuna:" + str(j.get("id")),
                    "title": j.get("title", "").replace("<strong>", "").replace("</strong>", ""),
                    "company": (j.get("company") or {}).get("display_name", "Unknown"),
                    "location": (j.get("location") or {}).get("display_name", ""),
                    "url": j.get("redirect_url", ""),
                    "posted": j.get("created", ""),
                    "source": "Adzuna",
                })
        except Exception as e:  # keep going if one search fails
            print(f"! Adzuna search failed for {s}: {e}")
    return jobs


def fetch_boards(cfg):
    boards = cfg.get("company_boards", {})
    jobs = []
    for slug in boards.get("greenhouse", []):
        try:
            data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            for j in data.get("jobs", []):
                jobs.append({
                    "id": f"gh:{slug}:{j['id']}", "title": j.get("title", ""),
                    "company": slug, "location": (j.get("location") or {}).get("name", ""),
                    "url": j.get("absolute_url", ""), "posted": j.get("updated_at", ""),
                    "source": "Company careers (Greenhouse)",
                })
        except Exception as e:
            print(f"! Greenhouse board '{slug}' failed: {e}")
    for slug in boards.get("lever", []):
        try:
            for j in http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
                ts = j.get("createdAt")
                posted = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else ""
                jobs.append({
                    "id": f"lever:{slug}:{j['id']}", "title": j.get("text", ""),
                    "company": slug, "location": (j.get("categories") or {}).get("location", ""),
                    "url": j.get("hostedUrl", ""), "posted": posted,
                    "source": "Company careers (Lever)",
                })
        except Exception as e:
            print(f"! Lever board '{slug}' failed: {e}")
    return jobs


# ---------------------------------------------------------------- filtering
def matches(job, cfg, from_board):
    title = job["title"].lower()
    if any(x.lower() in title for x in cfg.get("exclude_in_title", [])):
        return False
    if from_board:  # boards list ALL jobs, so require a role keyword and a location
        if not any(k.lower() in title for k in cfg.get("board_title_keywords", [])):
            return False
        locs = [l.lower() for l in cfg.get("board_locations", [])]
        if locs and not any(l in job["location"].lower() for l in locs):
            return False
    return True


# ---------------------------------------------------------------- notify
def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("(Telegram not configured) " + text.replace("\n", " | "))
        return
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        http_json(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    except Exception as e:
        print(f"! Telegram send failed: {e}")


def format_job(j):
    posted = j["posted"][:16].replace("T", " ") if j["posted"] else "date not shown"
    return (f"NEW JOB: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
            f"Posted: {posted}  |  Source: {j['source']}\nApply: {j['url']}")


# ---------------------------------------------------------------- main
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return None  # None = first run


def run_once():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    seen = load_seen()
    first_run = seen is None
    seen = seen or set()

    jobs = [j for j in fetch_adzuna(cfg) if matches(j, cfg, False)]
    jobs += [j for j in fetch_boards(cfg) if matches(j, cfg, True)]
    new = [j for j in jobs if j["id"] not in seen and j["url"]]
    new.sort(key=lambda j: j["posted"], reverse=True)

    to_send = new[:10] if first_run else new[:25]  # first run: avoid spamming old jobs
    for j in reversed(to_send):  # oldest first, so the newest ends up at the bottom
        send_telegram(format_job(j))
        time.sleep(0.5)
    seen.update(j["id"] for j in new)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)
    print(f"{datetime.now():%H:%M:%S} checked {len(jobs)} matching jobs, {len(new)} new, {len(to_send)} sent")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="repeat every N seconds (e.g. 600)")
    args = ap.parse_args()
    if not args.loop:
        run_once()
    else:
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"! run failed: {e}")
            time.sleep(args.loop)

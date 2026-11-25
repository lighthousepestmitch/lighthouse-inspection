#!/usr/bin/env python3
"""Bait check texts. Runs on Mitch's Mac every few minutes (LaunchAgent com.lighthouse.bait-texts).

Sends the "we've checked your bait stations" text from Mitch's number for checks the database has queued (bait_checks.text_status='queued', see
52_bait_tracker.sql). Rules: only after the calendar job is marked done, 10 minutes after, weekdays 7am to 6pm, once per check, never to a number on
the Job Book stop list, and only for real when "Send for real" is ticked in the Bait Tracker (otherwise it just records what it would have sent).
Signs in the same way as pull_inbox.py (password in the Keychain, never in a file).

  python3 bait_texts.py           normal run
  python3 bait_texts.py --dry     show what it would do, change nothing, send nothing
"""
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pull_inbox as P  # noqa: E402

HOLD_MIN = 10
TOO_OLD_DAYS = 3
HOME = pathlib.Path.home()
CHAT_DB = HOME / "Library" / "Messages" / "chat.db"
STOPLIST = pathlib.Path(__file__).resolve().parent.parent / "jobbook-data" / "stoplist.json"
LOG = HOME / "Library" / "Logs" / "lpc-bait-texts.log"

SCRIPT = '''on run argv
  tell application "Messages"
    set svc to 1st service whose service type = %s
    send (item 2 of argv) to buddy (item 1 of argv) of svc
  end tell
end run'''


def log(msg):
    line = "%s  %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(msg):
    subprocess.run(["osascript", "-e", "on run argv\ndisplay notification (item 1 of argv) with title \"Bait texts\"\nend run", msg], capture_output=True)


def in_hours(now):
    return now.weekday() < 5 and 7 <= now.hour < 18


def compose(template, site, staff):
    who = staff["name"].split()[0] if staff and staff.get("role") == "tech" else "I"
    return (template.replace("{name}", (site.get("contact_name") or "there").strip()).replace("{who}", who)
            .replace("{street}", site.get("address", "").split(",")[0].strip()))


def on_stoplist(mobile):
    try:
        text = STOPLIST.read_text(encoding="utf-8")
    except OSError:
        return False
    pattern = r"[\s\-]?".join(mobile[1:])
    return re.search(r"(?:\+?61|0)[\s\-]?" + pattern, text) is not None


def sent_state(to, body, since):
    """'sent', 'error', 'pending' or 'unknown' (chat.db can't be read) for the newest text to this number."""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % CHAT_DB, uri=True)
        rows = db.execute("select m.is_sent, m.error from message m join handle h on h.ROWID=m.handle_id "
                          "where m.is_from_me=1 and h.id=? and m.date > ? order by m.date desc limit 1",
                          (to, int((since - 978307200 - 3) * 1000000000))).fetchall()
        db.close()
    except sqlite3.Error:
        return "unknown"
    if not rows:
        return "pending"
    sent, error = rows[0]
    return "error" if error else ("sent" if sent else "pending")


def send_one(service, to, body):
    r = subprocess.run(["osascript", "-e", SCRIPT % service, to, body], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, r.stderr.strip()[:200]


def send_text(mobile, body):
    """iMessage first, plain SMS only if iMessage definitely failed (never both when it may still go). Returns (status, note)."""
    if os.environ.get("LPC_NEVER_SEND") == "1":
        return "failed", "LPC_NEVER_SEND=1"
    to = "+61" + mobile[1:]
    note = ""
    for service in ("iMessage", "SMS"):
        started = time.time()
        went, err = send_one(service, to, body)
        if not went:
            note = err
            continue
        state = "pending"
        for _ in range(12):
            time.sleep(2.5)
            state = sent_state(to, body, started)
            if state in ("sent", "error", "unknown"):
                break
        if state == "sent":
            return "sent", ""
        if state == "unknown":
            return "sent", "Handed to Messages, delivery not checked."
        if state == "pending":
            return "failed", "Messages hadn't confirmed it. Check Messages before sending again."
        note = "Messages reported an error."
    return "failed", note or "Messages didn't take it."


class Db:
    def __init__(self):
        self.base, anon = P.supabase_settings()
        cfg = json.loads(P.CONFIG.read_text(encoding="utf-8"))
        token = P.sign_in(self.base, anon, cfg["email"], P.get_password(cfg["email"]))
        self.head = {"apikey": anon, "Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def get(self, path):
        return json.loads(P.http("GET", self.base + "/rest/v1/" + path, self.head)[1])

    def patch(self, query, fields):
        head = dict(self.head, Prefer="return=representation")
        return json.loads(P.http("PATCH", self.base + "/rest/v1/bait_checks?" + query, head, fields)[1])


def run(db, dry=False, now=None, sender=send_text):
    now = now or dt.datetime.now()
    settings = db.get("bait_settings?id=eq.1&select=*")[0]
    if settings["texts_paused"]:
        return []
    if not in_hours(now) and not dry:
        return []
    utc = dt.datetime.now(dt.timezone.utc)
    cutoff = (utc - dt.timedelta(minutes=HOLD_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.get("bait_checks?text_status=eq.queued&done_at=lt." + urllib.parse.quote(cutoff) +
                  "&select=id,done_at,text_to,result,job:jobs(status),site:bait_sites(contact_name,address,auto_text,status),staff:staff(name,role)")
    out = []
    for c in rows:
        site, job, to = c.get("site") or {}, c.get("job") or {}, c.get("text_to") or ""
        street = site.get("address", "?").split(",")[0]
        if job.get("status") != "done":
            continue  # undone or not saved yet: wait
        only_queued = "id=eq.%s&text_status=eq.queued" % c["id"]
        done_at = dt.datetime.fromisoformat(c["done_at"].replace("Z", "+00:00"))
        body = compose(settings["template"], site, c.get("staff"))
        hold = None
        if c.get("result") != "not_active" or not site.get("auto_text") or not re.fullmatch(r"04\d{8}", to):
            hold = "Not set to send by itself."
        elif utc - done_at > dt.timedelta(days=TOO_OLD_DAYS):
            hold = "Waited too long to send. Send it yourself if still wanted."
        elif on_stoplist(to):
            hold = "On the stop list."
        if dry:
            log("DRY %s: %s" % (street, "hold, " + hold if hold else body))
        elif hold:
            db.patch(only_queued, {"text_status": "held", "text_error": hold})
        elif not settings["texts_live"]:
            db.patch(only_queued, {"text_status": "dry_run", "text_body": body})
            log("practice run, not sent: " + street)
        else:
            # claim it first, so a second run can never send the same one
            if not db.patch(only_queued, {"text_status": "sending", "text_body": body}):
                continue
            try:
                status, note = sender(to, body)
            except Exception as e:  # never leave a row stuck on "sending"
                status, note = "failed", "Check Messages before sending again. %s" % str(e)[:150]
            fields = {"text_status": status, "text_error": note or None}
            if status == "sent":
                fields["texted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            db.patch("id=eq.%s" % c["id"], fields)
            log("%s: %s %s" % (status, street, note))
            if status != "sent":
                notify("Text for %s didn't go. See the Bait Tracker." % street)
            time.sleep(4)
        out.append((street, body, hold))
    return out


def main():
    dry = "--dry" in sys.argv
    try:
        run(Db(), dry=dry)
    except P.Offline as e:
        log("offline: %s" % e)
    except Exception as e:
        log("stopped: %s" % str(e)[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""health_check.py: is everything on this Mac actually running? Written by Claude 2026-11-14.

READ-ONLY. It only looks at the logs and files the Mac jobs already write (nothing is changed, nothing goes over the internet,
nothing is written to any database), then writes a small summary to ~/Backups/health.json and prints it.

  python3 health_check.py            prints the summary, exit 0 all good, 1 something is red
  python3 health_check.py --json     prints the JSON

Checks: nightly Supabase backup, the 5-minute inbox pull, the Mon to Fri texts and emails copy (and whether macOS has blocked it).
"""
import datetime, json, os, re, subprocess, sys

HOME = os.path.expanduser("~")

def _age(now, then):
    s = int((now - then).total_seconds())
    if s < 90: return "just now"
    if s < 5400: return "%d min ago" % round(s / 60)
    if s < 172800: return "%d hours ago" % round(s / 3600)
    return "%d days ago" % round(s / 86400)

def _loaded(label):
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        return label in out
    except Exception:
        return None

def check_backup(now, home=HOME):
    p = os.path.join(home, "Backups", "lpc-supabase", "LAST_BACKUP.json")
    try:
        d = json.load(open(p)); when = datetime.datetime.fromisoformat(d["when"]).replace(tzinfo=None)
    except Exception:
        return {"id": "backup", "label": "Nightly backup", "status": "red", "detail": "No backup record found."}
    age_h = (now - when).total_seconds() / 3600
    if not d.get("ok"): return {"id": "backup", "label": "Nightly backup", "status": "red", "detail": "The last backup reported a problem (%s)." % _age(now, when)}
    if age_h > 26: return {"id": "backup", "label": "Nightly backup", "status": "red", "detail": "Last ran %s. It should run every night at 2:30am." % _age(now, when), "when": when.isoformat()}
    return {"id": "backup", "label": "Nightly backup", "status": "ok", "detail": "Ran %s." % _age(now, when), "when": when.isoformat()}

def check_inbox(now, home=HOME):
    log = os.path.join(home, "Library", "Logs", "lpc-inbox.log"); err = os.path.join(home, "Library", "Logs", "lpc-inbox.err")
    try:
        lines = open(log, errors="replace").read().splitlines()[-60:]
    except Exception:
        return {"id": "inbox", "label": "Report inbox pull", "status": "red", "detail": "No log found."}
    last = None
    for ln in reversed(lines):
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
        if m: last = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"); break
    try:
        err_recent = (now - datetime.datetime.fromtimestamp(os.path.getmtime(err))).total_seconds() < 3600
    except OSError:
        err_recent = False
    if err_recent: return {"id": "inbox", "label": "Report inbox pull", "status": "red", "detail": "It has been logging errors in the last hour (macOS may be blocking it)."}
    if last is None: return {"id": "inbox", "label": "Report inbox pull", "status": "red", "detail": "The log has no recent entries."}
    if (now - last).total_seconds() > 45 * 60:
        return {"id": "inbox", "label": "Report inbox pull", "status": "red", "detail": "Last ran %s. It should run every 5 minutes (is the Mac asleep?)." % _age(now, last), "when": last.isoformat()}
    return {"id": "inbox", "label": "Report inbox pull", "status": "ok", "detail": "Ran %s." % _age(now, last), "when": last.isoformat()}

def check_texts(now, home=HOME):
    root = os.path.join(home, "Backups", "chatdb-snapshots"); log = os.path.join(home, "Library", "Logs", "lpc-texts-copy.log")
    days = sorted(d for d in (os.listdir(root) if os.path.isdir(root) else []) if re.match(r"^\d{4}-\d{2}-\d{2}$", d) and os.path.isfile(os.path.join(root, d, "chat.db")))
    last = None
    if days:
        try: last = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(root, days[-1], "chat.db")))
        except OSError: last = datetime.datetime.strptime(days[-1], "%Y-%m-%d") + datetime.timedelta(hours=6, minutes=30)
    blocked = False
    try:
        tail = open(log, errors="replace").read()[-3000:]
        denied = "authorization denied" in tail or "Operation not permitted" in tail
        # an old error only counts if it is NEWER than the last good copy (a copy made by hand since clears it)
        blocked = denied and (last is None or datetime.datetime.fromtimestamp(os.path.getmtime(log)) > last)
    except OSError:
        pass
    # Mon to Fri 6:30am job: allow the weekend. Tue to Fri a day old is normal; Sat, Sun and Mon can be up to Friday's.
    allowed_h = 74 if now.weekday() in (5, 6, 0) else 30
    if blocked:
        return {"id": "texts", "label": "Texts and emails copy", "status": "red", "detail": "macOS is blocking it. Give the app 'Lighthouse Texts Copy' Full Disk Access (System Settings, Privacy and Security).", "when": last.isoformat() if last else None}
    if last is None: return {"id": "texts", "label": "Texts and emails copy", "status": "red", "detail": "No copy has been made yet."}
    if (now - last).total_seconds() / 3600 > allowed_h:
        return {"id": "texts", "label": "Texts and emails copy", "status": "red", "detail": "Last copy was %s. Texts you delete by hand since then are not saved anywhere." % _age(now, last), "when": last.isoformat()}
    return {"id": "texts", "label": "Texts and emails copy", "status": "ok", "detail": "Last copy %s." % _age(now, last), "when": last.isoformat()}

def run(now=None, home=HOME):
    now = now or datetime.datetime.now()
    checks = [check_backup(now, home), check_inbox(now, home), check_texts(now, home)]
    for label, cid in (("com.lighthouse.backup-supabase", "backup"), ("au.com.lighthousepest.inspection-inbox", "inbox"), ("com.lighthouse.texts-copy", "texts")):
        if _loaded(label) is False:
            c = next(x for x in checks if x["id"] == cid); c["status"] = "red"; c["detail"] += " The scheduled job is not loaded."
    overall = "red" if any(c["status"] == "red" for c in checks) else "ok"
    return {"made": now.isoformat(timespec="seconds"), "overall": overall, "checks": checks}

def main(argv):
    res = run()
    try:
        p = os.path.join(HOME, "Backups", "health.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(res, open(p, "w"), indent=1); os.chmod(p, 0o600)
    except OSError:
        pass
    if "--json" in argv: print(json.dumps(res, indent=1))
    else:
        print("Is it all running? %s" % ("Yes." if res["overall"] == "ok" else "NO, something needs a look."))
        for c in res["checks"]: print("  %-3s %-24s %s" % ("OK" if c["status"] == "ok" else "RED", c["label"], c["detail"]))
    return 0 if res["overall"] == "ok" else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

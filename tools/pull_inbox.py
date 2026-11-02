#!/usr/bin/env python3
"""Pull Inspection Capture jobs that were sent to the office into a folder on the Mac.

Written by Claude on 2026-11-02. Plain Python 3 (no installs).

What it does, each time it runs:
  1. Signs in to the office database as Mitch (same login as the Job Calendar).
  2. Looks for reports sent in over the last 7 days that it hasn't saved yet (or that were sent again with changes).
  3. For each one, downloads the photos and writes
        ~/Desktop/Inspection Inbox/<date> <address> (<who sent it>)/inspection_<address>_<date>.json
     in the same packet format as Finish & Export, so the Mira recipe works on it unchanged.
  4. Shows a Mac notification so you know it landed.

The password is never in a file. It lives in the Mac Keychain (service "lpc-inbox"), put there once by
inbox_setup.sh. The email is in ~/.lpc-inbox.json. The database address and public key come from ../lpc-config.js.

Usage:
  python3 pull_inbox.py            normal run (what the 5 minute timer does)
  python3 pull_inbox.py --days 30  also catch up on older reports
  python3 pull_inbox.py --verbose  print what it is doing
Test settings (not for real use): LPC_INBOX_ROOT moves every file location, LPC_INBOX_BASE_URL points at a stand-in
server, LPC_INBOX_PASSWORD skips the Keychain.
"""
import argparse
import base64
import datetime
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(os.environ.get("LPC_INBOX_ROOT") or pathlib.Path.home())
CONFIG = ROOT / ".lpc-inbox.json"
STATE = ROOT / ".lpc-inbox-state.json"
INBOX = ROOT / "Desktop" / "Inspection Inbox"
LOG = ROOT / "Library" / "Logs" / "lpc-inbox.log"
CFG_JS = pathlib.Path(__file__).resolve().parent.parent / "lpc-config.js"
BUCKET = "inspection-photos"
REF = "sbphoto:"
SERVICE = "lpc-inbox"
VERBOSE = False


def say(msg):
    """Print (when --verbose) and always append to the log. Never put keys or passwords in here."""
    line = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg
    if VERBOSE:
        print(msg)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title, text):
    if os.environ.get("LPC_INBOX_ROOT"):
        return
    try:
        script = 'display notification %s with title %s' % (json.dumps(text), json.dumps(title))
        subprocess.run(["osascript", "-e", script], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def supabase_settings():
    base = os.environ.get("LPC_INBOX_BASE_URL")
    txt = CFG_JS.read_text(encoding="utf-8") if CFG_JS.exists() else ""
    url = re.search(r'url:\s*"([^"]+)"', txt)
    key = re.search(r'anonKey:\s*"([^"]+)"', txt)
    if not (base or url) or not key:
        raise SystemExit("Can't find the database address in lpc-config.js")
    return (base or url.group(1)).rstrip("/"), key.group(1)


def get_password(email):
    pw = os.environ.get("LPC_INBOX_PASSWORD")
    if pw:
        return pw
    r = subprocess.run(["security", "find-generic-password", "-a", email, "-s", SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("No password in the Keychain yet. Run inbox_setup.sh once.")
    return r.stdout.rstrip("\n")


def http(method, url, headers, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s from the office (%s)" % (e.code, url.split("?")[0].split("/v1/")[-1][:60]))
    except urllib.error.URLError as e:
        raise RuntimeError("No connection (%s)" % str(e.reason)[:80])


def sign_in(base, anon, email, password):
    st, body, _ = http("POST", base + "/auth/v1/token?grant_type=password",
                       {"apikey": anon, "Content-Type": "application/json"},
                       {"email": email, "password": password})
    tok = json.loads(body).get("access_token")
    if not tok:
        raise RuntimeError("Login didn't give back a token")
    return tok


def api(base, anon, token, path):
    st, body, ctype = http("GET", base + path, {"apikey": anon, "Authorization": "Bearer " + token})
    return body, ctype


def walk(o, fn):
    if isinstance(o, str):
        return fn(o)
    if isinstance(o, list):
        return [walk(x, fn) for x in o]
    if isinstance(o, dict):
        return {k: walk(v, fn) for k, v in o.items()}
    return o


def drop_blank(o):
    if isinstance(o, list):
        return [drop_blank(x) for x in o if not (isinstance(x, str) and x == "")]
    if isinstance(o, dict):
        return {k: drop_blank(v) for k, v in o.items()}
    return o


def safe(s, n=40):
    return re.sub(r"[^\w]+", "_", s or "").strip("_")[:n]


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    cfg = load_json(CONFIG, {})
    email = cfg.get("email")
    if not email:
        raise SystemExit("No email set. Run inbox_setup.sh once.")
    base, anon = supabase_settings()
    try:
        token = sign_in(base, anon, email, get_password(email))
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = "/rest/v1/inspections?select=id,updated_at,staff_id,summary:data->summary&updated_at=gte.%s&order=updated_at.desc&limit=100" % urllib.parse.quote(since)
        rows = json.loads(api(base, anon, token, q)[0])
        names = {s["id"]: s["name"] for s in json.loads(api(base, anon, token, "/rest/v1/staff?select=id,name")[0])}
    except RuntimeError as e:
        say("Skipped this run: %s" % e)
        return 0

    state = load_json(STATE, {"rows": {}})
    done = state.setdefault("rows", {})
    todo = [r for r in rows if r.get("staff_id") and done.get(r["id"], {}).get("at") != r["updated_at"]]
    say("%d report(s) in the last %d days, %d to save" % (len(rows), args.days, len(todo)))
    for r in todo:
        rid = r["id"]
        try:
            full = json.loads(api(base, anon, token, "/rest/v1/inspections?id=eq.%s&select=id,updated_at,data" % urllib.parse.quote(rid))[0])[0]
            data = full["data"]
            packet = data["packet"]
            cleared = bool((data.get("summary") or {}).get("photosCleared"))
            refs = []
            walk(packet, lambda s: (refs.append(s) if s.startswith(REF) and s not in refs else None) or s)
            got = {}
            if not cleared:
                for ref in refs:
                    body, ctype = api(base, anon, token, "/storage/v1/object/authenticated/%s/%s" % (BUCKET, urllib.parse.quote(ref[len(REF):])))
                    mime = (ctype or "").split(";")[0] or mimetypes.guess_type(ref)[0] or "image/jpeg"
                    got[ref] = "data:%s;base64,%s" % (mime, base64.b64encode(body).decode())
            built = walk(packet, lambda s: got.get(s, "") if s.startswith(REF) else s)
            if cleared:
                built = drop_blank(built)
        except (RuntimeError, KeyError, IndexError, ValueError) as e:
            say("Couldn't save report %s: %s" % (rid[:8], e))
            continue
        cust = built.get("customer") or {}
        sent = (data.get("sentAt") or full["updated_at"])[:10]
        who = names.get(r["staff_id"], "someone")
        addr = cust.get("address") or cust.get("client") or ("job_" + rid[-6:])
        prev = done.get(rid)
        folder = INBOX / (prev["folder"] if prev else "%s %s (%s)" % (sent, safe(addr, 40).replace("_", " "), who))
        folder.mkdir(parents=True, exist_ok=True)
        fname = "inspection_%s_%s.json" % (safe(addr) or "inspection", sent)
        tmp = folder / (fname + ".part")
        tmp.write_text(json.dumps(built), encoding="utf-8")
        tmp.replace(folder / fname)
        summ = data.get("summary") or {}
        (folder / "About this job.txt").write_text(
            "Sent by %s on %s\nAddress: %s\nClient: %s\nFindings: %s   Photos: %s%s\nSaved by the inbox script. Say \"build it\" to Claude.\n"
            % (who, sent, cust.get("address", ""), cust.get("client", ""), summ.get("find", "?"), summ.get("photos", "?"),
               "   (photos were cleared from the office copy)" if cleared else ""), encoding="utf-8")
        done[rid] = {"at": r["updated_at"], "folder": folder.name}
        STATE.write_text(json.dumps(state), encoding="utf-8")
        say("Saved %s -> %s" % (rid[:8], folder.name))
        notify("Job landed in your Inspection Inbox", "%s sent %s" % (who, addr))
    return 0


if __name__ == "__main__":
    sys.exit(main())

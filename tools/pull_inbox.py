#!/usr/bin/env python3
"""Pull Inspection Capture jobs that were sent to the office into a folder on the Mac.

Written by Claude on 2026-11-02, hardened on 2026-11-04 (see pull_inbox_new_notes.txt). Plain Python 3, no installs.

What it does, each time it runs:
  1. Signs in to the office database as Mitch (same login as the Job Calendar).
  2. Looks for reports sent in over the last 7 days that it hasn't saved yet (or that were sent again with changes).
  3. For each one, downloads the photos and writes
        ~/Desktop/Inspection Inbox/<ADDRESS> - <date> - <who sent it>/inspection_<address>_<date>.json
     in the same packet format as Finish & Export, so the Mira recipe works on it unchanged.
     The date is the Sydney date the job was sent from the field.
  4. Shows a Mac notification so you know it landed.

Rules it keeps:
  - One job failing never stops the others. Each failure goes in the log with the job id and the exact error.
    After 3 failed runs in a row it shows a Mac notification (at most once an hour).
  - A "photos cleared" update never replaces a packet that still has photos in it.
  - If Mitch deletes a job folder it stays deleted. It only comes back if the job is sent again from the field
    (a newer send with photos), never because the office copy's photos were cleared.
  - Old folders keep their names. A folder is only renamed when that job's address changes.

The password is never in a file. It lives in the Mac Keychain (service "lpc-inbox"), put there once by
inbox_setup.sh. The email is in ~/.lpc-inbox.json. The database address and public key come from ../lpc-config.js.

Usage:
  python3 pull_inbox_new.py                 normal run (what the 5 minute timer does)
  python3 pull_inbox_new.py --days 30       also catch up on older reports
  python3 pull_inbox_new.py --verbose       print what it is doing
  python3 pull_inbox_new.py --dry-run       say what it would save, rename, keep or skip, and write nothing at all
  python3 pull_inbox_new.py --restore-deleted   bring back job folders that were deleted by hand (this run only)
Where things go (for testing): --inbox DIR (or LPC_INBOX_DIR), --state FILE (or LPC_INBOX_STATE),
  --log FILE (or LPC_INBOX_LOG), --no-notify (or LPC_INBOX_NO_NOTIFY=1).
Older test settings still work: LPC_INBOX_ROOT moves every file location, LPC_INBOX_BASE_URL points at a stand-in
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
import socket
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Australia/Sydney")
except Exception:  # no time zone data: fall back to the Mac's own clock zone (set to Sydney)
    TZ = None

ROOT = pathlib.Path(os.environ.get("LPC_INBOX_ROOT") or pathlib.Path.home())
CONFIG = ROOT / ".lpc-inbox.json"
STATE = pathlib.Path(os.environ.get("LPC_INBOX_STATE") or ROOT / ".lpc-inbox-state.json")
INBOX = pathlib.Path(os.environ.get("LPC_INBOX_DIR") or ROOT / "Desktop" / "Inspection Inbox")
LOG = pathlib.Path(os.environ.get("LPC_INBOX_LOG") or ROOT / "Library" / "Logs" / "lpc-inbox.log")
CFG_JS = pathlib.Path(__file__).resolve().parent.parent / "lpc-config.js"
BUCKET = "inspection-photos"
REF = "sbphoto:"
SERVICE = "lpc-inbox"
FAILS_BEFORE_ALERT = 3
ALERT_GAP = datetime.timedelta(hours=1)
ADDR_MAX = 60
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff"}
LEGACY_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2} (.*) \(([^()]*)\)$")   # "2026-11-03 12 Example Street (Mitch)"

VERBOSE = False
DRY = False
QUIET_NOTIFY = False


class Offline(RuntimeError):
    """No internet (or the office database didn't answer). Not counted as the puller failing."""


# ---------- small helpers ----------

def say(msg):
    """Print (when --verbose) and append to the log. Never put keys or passwords in here. Dry runs only print."""
    if VERBOSE or DRY:
        print(msg)
    if DRY:
        return
    line = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title, text):
    if DRY:
        return
    if QUIET_NOTIFY:
        say("(test mode, notification not shown) %s: %s" % (title, text))
        return
    try:
        script = 'display notification %s with title %s' % (json.dumps(text), json.dumps(title))
        subprocess.run(["osascript", "-e", script], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def err_text(e):
    return "%s: %s" % (type(e).__name__, e)


def parse_time(s):
    """ISO time from the database or the phone (any number of decimal places, Z or +00:00) to an aware datetime."""
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
    if m:
        frac = (m.group(2) or "")[:6].ljust(6, "0") if m.group(2) else ""
        s = m.group(1) + ("." + frac if frac else "") + m.group(3)
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def local(d):
    return d.astimezone(TZ) if TZ else d.astimezone()


def nice_time(d):
    d = local(d)
    return "%s %d %s at %d:%02d %s" % (d.strftime("%a"), d.day, d.strftime("%b %Y"), (d.hour + 11) % 12 + 1,
                                       d.minute, "am" if d.hour < 12 else "pm")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def supabase_settings():
    base = os.environ.get("LPC_INBOX_BASE_URL")
    txt = CFG_JS.read_text(encoding="utf-8") if CFG_JS.exists() else ""
    url = re.search(r'url:\s*"([^"]+)"', txt)
    key = re.search(r'anonKey:\s*"([^"]+)"', txt)
    if not (base or url) or not key:
        raise RuntimeError("Can't find the database address in lpc-config.js")
    return (base or url.group(1)).rstrip("/"), key.group(1)


def get_password(email):
    pw = os.environ.get("LPC_INBOX_PASSWORD")
    if pw:
        return pw
    r = subprocess.run(["security", "find-generic-password", "-a", email, "-s", SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("No password in the Keychain yet. Run inbox_setup.sh once.")
    return r.stdout.rstrip("\n")


def http(method, url, headers, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    where = url.split("?")[0].split("/v1/")[-1][:60]
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s from the office (%s)" % (e.code, where))
    except urllib.error.URLError as e:
        raise Offline("No connection (%s)" % str(e.reason)[:80])
    except (socket.timeout, TimeoutError, ConnectionError) as e:
        raise Offline("No connection (%s)" % str(e)[:80])


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


def write_atomic(path, data):
    """Write to <name>.part then swap it in. If the swap fails, tidy the .part away and pass the error up."""
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------- names ----------

ILLEGAL = re.compile(r'[\\:*?"<>|\x00-\x1f\x7f]')


def clean_name(s):
    s = re.sub(r"\s*/\s*", "-", s or "")          # 1/3 Gara Court -> 1-3 Gara Court
    s = ILLEGAL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).replace(" ,", ",")
    return s.strip(" .")


def cap(s, n):
    if len(s) <= n:
        return s
    cut = s[:n]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip(" ,.-")


def address_part(cust, summ, rid):
    """ADDRESS IN CAPITALS for the folder name: number + street + suburb, else client, else JOB <6 of id>."""
    cust, summ = cust or {}, summ or {}
    no = str(cust.get("streetno") or "").strip()
    street = str(cust.get("address") or "").strip() or str(summ.get("address") or "").strip()
    suburb = str(cust.get("suburb") or "").strip() or str(summ.get("suburb") or "").strip()
    if street:
        first = street.split(" ", 1)[0].lower()
        if no and first != no.lower():   # the packet normally has the number folded in already
            street = no + " " + street
        text = street
        if suburb and suburb.lower() not in street.lower():
            text = street + ", " + suburb
    else:
        text = str(cust.get("client") or "").strip() or str(summ.get("client") or "").strip()
    text = cap(clean_name(text.upper()), ADDR_MAX)
    return text or "JOB " + rid[-6:].upper()


def legacy_address(cust, rid):
    """The address bit of an old style folder name (what pull_inbox.py used before 2026-11-04)."""
    cust = cust or {}
    addr = cust.get("address") or cust.get("client") or ("job_" + rid[-6:])
    return safe(addr, 40).replace("_", " ")


def packet_name(cust, rid, day):
    cust = cust or {}
    addr = cust.get("address") or cust.get("client") or ("job_" + rid[-6:])
    return "inspection_%s_%s.json" % (safe(addr) or "inspection", day)


def full_address(cust, summ):
    cust, summ = cust or {}, summ or {}
    street = cust.get("address") or summ.get("address") or ""
    tail = " ".join(x for x in [cust.get("state") or "", cust.get("postcode") or ""] if x)
    sub = cust.get("suburb") or summ.get("suburb") or ""
    out = ", ".join(x for x in [street, " ".join(x for x in [sub, tail] if x)] if x)
    return out


def about_text(addr_caps, who, sent, cust, summ, cleared, status=None):
    cust, summ = cust or {}, summ or {}
    lines = [
        "%s  (sent by %s, %s)" % (addr_caps, who, nice_time(sent)),
        "",
        "Client: %s" % (cust.get("client") or summ.get("client") or ""),
        "Address: %s" % full_address(cust, summ),
        "Findings: %s   Photos: %s%s" % (summ.get("find", "?"), summ.get("photos", "?"),
                                         "   (photos were cleared from the office copy)" if cleared else ""),
    ]
    if status:
        lines.append(status)
    lines.append("Saved by the inbox script on %s. Say \"build it\" to Claude." % nice_time(now_utc()))
    return "\n".join(lines) + "\n"


# ---------- what is already on the Mac ----------

def folder_state(name):
    """'here', 'gone' or 'unknown' (couldn't look, so never treat it as deleted)."""
    try:
        os.stat(str(INBOX / name))
        return "here"
    except FileNotFoundError:
        return "gone"
    except OSError:
        return "unknown"


def local_has_photos(folder):
    """True if the folder holds photo files or a packet with photos in it. Any doubt counts as yes (keep it)."""
    try:
        for p in folder.rglob("*"):
            if p.suffix.lower() in PHOTO_EXT and p.is_file():
                return True
        for p in folder.glob("inspection_*.json"):
            with open(p, "rb") as f:
                tail = b""
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    if b"data:image" in tail + chunk:
                        return True
                    tail = chunk[-16:]
        return False
    except OSError:
        return True


def check_deleted(rows_state):
    """Mark jobs whose folder has gone (deleted by hand) and un-mark any that came back."""
    inbox_gone = folder_state("") == "gone"
    if inbox_gone and any(st.get("folder") and not st.get("deleted") for st in rows_state.values()):
        say("The Inspection Inbox folder itself is missing, so every job in it counts as deleted.")
    for rid, st in rows_state.items():
        name = st.get("folder")
        if not name:
            continue
        where = "gone" if inbox_gone else folder_state(name)
        if st.get("deleted"):
            if where == "here":
                st.pop("deleted", None)
                say("Job %s: folder \"%s\" is back, so it's no longer marked as deleted." % (rid, name))
            continue
        if where == "gone":
            st["deleted"] = now_utc().isoformat()
            say("Job %s: folder \"%s\" was deleted by hand. It won't come back unless the job is sent again from the field."
                % (rid, name))


# ---------- health (3 failed runs in a row -> one notification an hour) ----------

def health_path():
    return STATE.with_name(STATE.stem + "-health.json")


def record_run(failed, detail=""):
    if DRY:
        return
    hp = health_path()
    h = load_json(hp, {})
    if failed:
        h["fails"] = int(h.get("fails") or 0) + 1
        h["last_fail"] = now_utc().isoformat()
        say("This run failed%s. Failed runs in a row: %d" % ((" (%s)" % detail) if detail else "", h["fails"]))
        last = parse_time(h.get("last_alert"))
        if h["fails"] >= FAILS_BEFORE_ALERT and (not last or now_utc() - last >= ALERT_GAP):
            say("Showing the 'inbox puller is failing' notification.")
            notify("Inspection Inbox is failing",
                   "Jobs are not saving (%d runs in a row). Look in ~/Library/Logs/lpc-inbox.log" % h["fails"])
            h["last_alert"] = now_utc().isoformat()
    else:
        if h.get("fails"):
            say("Back to normal after %d failed run(s)." % h["fails"])
        h["fails"] = 0
        h["last_ok"] = now_utc().isoformat()
    try:
        write_atomic(hp, json.dumps(h).encode())
    except OSError as e:
        say("Couldn't save the run record %s: %s" % (hp, err_text(e)))


# ---------- the run ----------

class Run:
    def __init__(self, base, anon, token, names, state, restore_deleted):
        self.base, self.anon, self.token, self.names = base, anon, token, names
        self.state = state
        self.rows = state.setdefault("rows", {})
        self.restore_deleted = restore_deleted

    def save_state(self):
        if DRY:
            return
        write_atomic(STATE, json.dumps(self.state).encode())

    def taken(self, name, rid, current):
        low = name.lower()
        if current and current.lower() == low:
            return False
        for orid, ost in self.rows.items():
            if orid != rid and not ost.get("deleted") and (ost.get("folder") or "").lower() == low:
                return True
        return os.path.lexists(str(INBOX / name))

    def unique(self, label, rid, current=None):
        for c in [label, "%s (%s)" % (label, rid[-6:].upper()), "%s (%s)" % (label, rid.upper())]:
            if not self.taken(c, rid, current):
                return c
        n = 2
        while self.taken("%s (%s %d)" % (label, rid.upper(), n), rid, current):
            n += 1
        return "%s (%s %d)" % (label, rid.upper(), n)

    def describe(self, r):
        rid = r["id"]
        summ = r.get("summary") or {}
        cust = r.get("customer") or {}
        sent = parse_time(r.get("sent")) or parse_time(r.get("updated_at")) or now_utc()
        who = clean_name(self.names.get(r.get("staff_id"), "Someone")) or "Someone"
        addr = address_part(cust, summ, rid)
        label = "%s - %s - %s" % (addr, local(sent).strftime("%Y-%m-%d"), who)
        return {"rid": rid, "summ": summ, "cust": cust, "sent": sent, "who": who, "addr": addr, "label": label,
                "cleared": bool(summ.get("photosCleared"))}

    def address_changed(self, st, info):
        if st.get("addr"):
            return st["addr"] != info["addr"]
        m = LEGACY_FOLDER.match(st.get("folder") or "")
        if not m:
            return False   # can't tell, so leave the name alone
        return m.group(1).strip().lower() != legacy_address(info["cust"], info["rid"]).strip().lower()

    def plan(self, r):
        """Decide what to do with one row: none, skip-deleted, keep, save."""
        rid = r["id"]
        st = self.rows.get(rid)
        info = self.describe(r)
        if st and st.get("deleted") and not self.restore_deleted:
            if st.get("at") == r["updated_at"]:
                return "none", info
            base = parse_time(st.get("sent")) or parse_time(st.get("at"))
            if not info["cleared"] and (base is None or info["sent"] > base):
                return "save", info          # a genuinely newer send from the field
            return "skip-deleted", info
        if st and not st.get("deleted") and st.get("at") == r["updated_at"]:
            return "none", info
        if st and st.get("folder") and not st.get("deleted") and info["cleared"] \
                and local_has_photos(INBOX / st["folder"]):
            return "keep", info
        return "save", info

    def fetch(self, rid):
        full = json.loads(api(self.base, self.anon, self.token,
                              "/rest/v1/inspections?id=eq.%s&select=id,updated_at,data" % urllib.parse.quote(rid))[0])[0]
        data = full["data"]
        packet = data["packet"]
        cleared = bool((data.get("summary") or {}).get("photosCleared"))
        refs = []
        walk(packet, lambda s: (refs.append(s) if s.startswith(REF) and s not in refs else None) or s)
        got = {}
        if not cleared:
            for ref in refs:
                body, ctype = api(self.base, self.anon, self.token, "/storage/v1/object/authenticated/%s/%s"
                                  % (BUCKET, urllib.parse.quote(ref[len(REF):])))
                mime = (ctype or "").split(";")[0] or mimetypes.guess_type(ref)[0] or "image/jpeg"
                got[ref] = "data:%s;base64,%s" % (mime, base64.b64encode(body).decode())
        built = walk(packet, lambda s: got.get(s, "") if s.startswith(REF) else s)
        if cleared:
            built = drop_blank(built)
        return full, data, built, cleared

    def keep(self, r, info):
        """Office copy's photos were cleared but this folder still has them: leave the packet, refresh the note."""
        rid = r["id"]
        st = self.rows[rid]
        folder = INBOX / st["folder"]
        status = ("The office copy's photos were cleared on %s. This folder still has them, so the packet here was "
                  "left as it was." % nice_time(parse_time(info["summ"].get("photosCleared")) or now_utc()))
        write_atomic(folder / "About this job.txt",
                     about_text(info["addr"], info["who"], info["sent"], info["cust"], info["summ"], False, status).encode())
        st["at"] = r["updated_at"]
        st["cleared_kept"] = True
        self.save_state()
        say("Kept %s (%s): photos cleared in the office copy, but this folder still has them" % (rid, st["folder"]))

    def skip_deleted(self, r, info):
        st = self.rows[r["id"]]
        st["at"] = r["updated_at"]
        self.save_state()
        say("Skipped %s: its folder was deleted by hand and this update isn't a new send from the field%s"
            % (r["id"], " (photos cleared)" if info["cleared"] else ""))

    def save(self, r, info):
        rid = r["id"]
        st = self.rows.get(rid)
        full, data, built, cleared = self.fetch(rid)
        cust = built.get("customer") or {}
        summ = data.get("summary") or {}
        info = dict(info, cust=cust, summ=summ, cleared=cleared,
                    sent=parse_time(data.get("sentAt")) or parse_time(full.get("updated_at")) or info["sent"])
        info["addr"] = address_part(cust, summ, rid)
        info["label"] = "%s - %s - %s" % (info["addr"], local(info["sent"]).strftime("%Y-%m-%d"), info["who"])
        day = local(info["sent"]).strftime("%Y-%m-%d")
        fname = packet_name(cust, rid, day)
        live = st and st.get("folder") and not st.get("deleted") and folder_state(st["folder"]) == "here"
        if live:
            name = st["folder"]
            if self.address_changed(st, info):
                new = self.unique(info["label"], rid, current=name)
                if new != name:
                    os.rename(str(INBOX / name), str(INBOX / new))
                    st["folder"], st["addr"] = new, info["addr"]
                    self.save_state()      # straight away, so a later failure can't look like a deleted folder
                    say("Renamed %s: \"%s\" -> \"%s\" (address changed)" % (rid, name, new))
                    name = new
            st["addr"] = info["addr"]
        else:
            name = self.unique(info["label"], rid)
            INBOX.mkdir(parents=True, exist_ok=True)
            (INBOX / name).mkdir()
            st = self.rows[rid] = {"at": None, "folder": name, "addr": info["addr"]}
            self.save_state()              # remember the folder is ours before writing into it
        folder = INBOX / name
        write_atomic(folder / fname, json.dumps(built).encode("utf-8"))
        old = st.get("file")
        if old and old != fname:
            try:
                (folder / old).unlink()    # the older packet this script wrote for the same job
            except FileNotFoundError:
                pass
        write_atomic(folder / "About this job.txt",
                     about_text(info["addr"], info["who"], info["sent"], cust, summ, cleared).encode())
        st.update({"at": r["updated_at"], "folder": name, "addr": info["addr"], "file": fname,
                   "sent": info["sent"].isoformat(), "cleared": cleared, "saved": now_utc().isoformat()})
        st.pop("deleted", None)
        st.pop("cleared_kept", None)
        self.save_state()
        say("Saved %s -> %s" % (rid, name))
        notify("Job landed in your Inspection Inbox", "%s sent %s" % (info["who"], info["addr"]))


def run(args):
    cfg = load_json(CONFIG, {})
    email = cfg.get("email")
    if not email:
        raise RuntimeError("No email set. Run inbox_setup.sh once.")
    base, anon = supabase_settings()
    token = sign_in(base, anon, email, get_password(email))
    since = (now_utc() - datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = ("/rest/v1/inspections?select=id,updated_at,staff_id,summary:data->summary,sent:data->>sentAt,"
         "customer:data->packet->customer&updated_at=gte.%s&order=updated_at.asc&limit=100" % urllib.parse.quote(since))
    rows = json.loads(api(base, anon, token, q)[0])
    names = {s["id"]: s["name"] for s in json.loads(api(base, anon, token, "/rest/v1/staff?select=id,name")[0])}

    if STATE.exists():
        state = load_json(STATE, None)
        if not isinstance(state, dict):
            raise RuntimeError("The state file %s can't be read, so nothing was saved (to avoid doubling up folders)" % STATE)
    else:
        state = {"rows": {}}
    R = Run(base, anon, token, names, state, args.restore_deleted)
    check_deleted(R.rows)
    if not DRY:
        R.save_state()

    rows = [r for r in rows if r.get("staff_id")]
    plans = [(r,) + R.plan(r) for r in rows]
    todo = [p for p in plans if p[1] != "none"]
    say("%d report(s) in the last %d days, %d to save" % (len(rows), args.days, len(todo)))

    if DRY:
        for r, what, info in plans:
            st = R.rows.get(r["id"]) or {}
            print("")
            print("Job %s  (%s, sent %s)" % (r["id"], info["who"], nice_time(info["sent"])))
            print("  Folder now:        %s" % ((st.get("folder") or "(none)") + ("   [deleted by hand]" if st.get("deleted") else "")))
            print("  Name, new rules:   %s" % info["label"])
            if what == "none":
                print("  Would do:          nothing (no change since it was last saved)")
            elif what == "skip-deleted":
                print("  Would do:          skip it (folder deleted by hand, and this isn't a new send from the field)")
            elif what == "keep":
                print("  Would do:          keep the packet (office photos cleared, this folder still has them), refresh About this job.txt")
            else:
                live = st.get("folder") and not st.get("deleted") and folder_state(st["folder"]) == "here"
                photos = "no photos (cleared)" if info["cleared"] else "%s photo(s) to download" % info["summ"].get("photos", "?")
                if live and R.address_changed(st, info):
                    print("  Would do:          rename to \"%s\" (address changed) and update the packet, %s"
                          % (R.unique(info["label"], r["id"], current=st["folder"]), photos))
                elif live:
                    print("  Would do:          update the packet in the same folder, %s" % photos)
                else:
                    print("  Would do:          make folder \"%s\", %s" % (R.unique(info["label"], r["id"]), photos))
        return 0

    failures = 0
    offline = False
    for r, what, info in todo:
        rid = r["id"]
        try:
            if what == "keep":
                R.keep(r, info)
            elif what == "skip-deleted":
                R.skip_deleted(r, info)
            else:
                R.save(r, info)
        except Offline as e:
            offline = True
            say("Couldn't save report %s: %s (will try again next run)" % (rid, e))
        except Exception as e:     # OSError (permissions, disk) and anything unexpected: log it and carry on
            failures += 1
            say("Couldn't save report %s: %s" % (rid, err_text(e)))
            traceback.print_exc(file=sys.stderr)
    if failures:
        return "failed", "%d job(s) didn't save" % failures
    return ("offline", "") if offline else ("ok", "")


def main():
    global VERBOSE, DRY, QUIET_NOTIFY, INBOX, STATE, LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen, write nothing")
    ap.add_argument("--inbox", help="folder to save jobs in (default ~/Desktop/Inspection Inbox)")
    ap.add_argument("--state", help="state file (default ~/.lpc-inbox-state.json)")
    ap.add_argument("--log", help="log file (default ~/Library/Logs/lpc-inbox.log)")
    ap.add_argument("--no-notify", action="store_true", help="log notifications instead of showing them")
    ap.add_argument("--restore-deleted", action="store_true", help="save jobs again even if their folder was deleted")
    args = ap.parse_args()
    VERBOSE, DRY = args.verbose, args.dry_run
    QUIET_NOTIFY = bool(args.no_notify or os.environ.get("LPC_INBOX_NO_NOTIFY") or os.environ.get("LPC_INBOX_ROOT"))
    if args.inbox:
        INBOX = pathlib.Path(args.inbox).expanduser()
    if args.state:
        STATE = pathlib.Path(args.state).expanduser()
    if args.log:
        LOG = pathlib.Path(args.log).expanduser()
    if DRY:
        print("DRY RUN: nothing will be written. Inbox %s, state %s" % (INBOX, STATE))

    try:
        result = run(args)
    except Offline as e:
        say("Skipped this run: %s" % e)
        return 0
    except Exception as e:
        say("Run stopped: %s" % err_text(e))
        traceback.print_exc(file=sys.stderr)
        record_run(True, err_text(e)[:120])
        return 1
    if DRY:
        return 0
    status, detail = result
    if status != "offline":
        record_run(status == "failed", detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())

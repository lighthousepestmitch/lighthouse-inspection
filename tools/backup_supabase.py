#!/usr/bin/env python3
"""Nightly read-only backup of the Job Calendar database (Supabase) onto this Mac.

Written by Claude on 2026-11-04. Plain Python 3.9 (what the Mac has), no installs.

Why: the Supabase project is on the free plan with no automatic backups, and the Job Calendar is the only
copy of the schedule, clients, reports and service records. This gives Mitch his own copy.

What it does, each run:
  1. Signs in as the master user (same login and Keychain item as pull_inbox.py).
  2. Reads every table, all rows, and writes one <table>.json per table.
  3. Lists everything in the two photo buckets (storage_<bucket>.json). With --photos it also downloads the files.
  4. Writes manifest.json (row counts and a sha256 for every file) and REPORT.txt (plain summary).
  5. Keeps the newest 14 backup folders and removes older ones (only folders this script made).

It only ever READS from Supabase. The only POSTs it can send are the sign-in and the storage "list" call, and the
code refuses anything else. No keys, passwords or tokens are written to any file or printed.

The backup holds customer names, phones and emails. It goes OUTSIDE every git repo, and the script refuses to run
if the destination is inside a git working tree.

Usage:
  python3 backup_supabase.py                      normal run, into ~/Backups/lpc-supabase/<date_time>/
  python3 backup_supabase.py --dest /Volumes/X/lpc-backups     put it on the external drive instead
  python3 backup_supabase.py --photos             also download the photo files (big)
  python3 backup_supabase.py --photos --since 3   only photos added or changed in the last 3 days
  python3 backup_supabase.py --dry-run            show what it would do, change nothing, no sign-in
  python3 backup_supabase.py --verify             re-check the newest backup against its manifest
  python3 backup_supabase.py --verify FOLDER      same, for one folder
  python3 backup_supabase.py --restore-check      check the newest backup parses and links up (no Supabase)
  python3 backup_supabase.py --prune-only         just remove old folders (add --dry-run to preview)

Exit codes: 0 all good, 1 failed (or verify/check found a problem), 2 finished but something could not be read.
"""
import argparse
import base64
import datetime
import errno
import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # not on a Mac, locking is skipped
    fcntl = None

VERSION = "1.0"
SCRIPT_NAME = "backup_supabase.py"
HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CFG_JS = REPO_ROOT / "lpc-config.js"
HOME = pathlib.Path.home()
USER_CONFIG = HOME / ".lpc-inbox.json"          # holds the login email only (same file pull_inbox.py uses)
KEYCHAIN_SERVICE = "lpc-inbox"                  # same Keychain item pull_inbox.py uses
DEFAULT_DEST = HOME / "Backups" / "lpc-supabase"
KEEP_DEFAULT = 14
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}(\d{2})?$")   # backup folder names, e.g. 2026-11-04_0230
PARTIAL_PREFIX = ".partial_"
PARTIAL_MARKER = ".lpc-backup-partial"
LOCK_NAME = ".backup.lock"

# Built from the SQL files in ~/lighthouse-supabase (01, 04, 08, 09, 14). name, sort columns (the primary key), page size.
# Small page sizes on the tables that hold big JSON so one page never gets huge.
TABLES = [
    ("staff", "id", 1000),
    ("staff_prefs", "staff_id,key", 1000),
    ("clients", "id", 1000),
    ("client_properties", "id", 1000),
    ("service_types", "id", 1000),
    ("service_prices", "service_type_id", 1000),
    ("jobs", "id", 1000),
    ("job_prices", "job_id", 1000),
    ("job_photos", "id", 1000),
    ("service_records", "id", 100),
    ("inspections", "id", 25),
    ("jobbook_state", "key", 200),
    ("jobbook_files", "name", 5),
    ("plan_blocks", "id", 1000),
    ("day_notes", "note_date", 1000),
]
BUCKETS = ["job-photos", "inspection-photos"]

# Links that are real database rules (foreign keys). An orphan here is a genuine problem.
# (child table, child column, parent table, parent column)
HARD_LINKS = [
    ("jobs", "client_id", "clients", "id"),
    ("jobs", "staff_id", "staff", "id"),
    ("job_photos", "job_id", "jobs", "id"),
    ("inspections", "job_id", "jobs", "id"),
    ("service_records", "job_id", "jobs", "id"),
    ("client_properties", "client_id", "clients", "id"),
    ("job_prices", "job_id", "jobs", "id"),
    ("service_prices", "service_type_id", "service_types", "id"),
    ("staff_prefs", "staff_id", "staff", "id"),
]
# Links with no database rule (just text ids). Shown for information only.
SOFT_LINKS = [
    ("inspections", "client_id", "clients", "id"),
    ("service_records", "client_id", "clients", "id"),
]

SYD = None
try:
    from zoneinfo import ZoneInfo
    SYD = ZoneInfo("Australia/Sydney")
except Exception:
    SYD = None


class BackupError(Exception):
    """A clear, plain-words failure. The message is safe to print (no keys, passwords or tokens)."""


# ---------------------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------------------

def say(msg=""):
    print(msg, flush=True)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def to_sydney(dt):
    return dt.astimezone(SYD) if SYD else dt.astimezone()


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def notify(title, text):
    """Mac notification, only when running without a terminal (the nightly timer) so a failure is seen."""
    try:
        script = "display notification %s with title %s" % (json.dumps(text), json.dumps(title))
        subprocess.run(["osascript", "-e", script], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def atomic_write(path, data, mode=0o600):
    """Write bytes to <path>.part then rename into place. File is private (600)."""
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + ".part")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(path))
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


def rows_to_bytes(rows):
    """One valid JSON array, one row per line (easy to grep and to eyeball)."""
    if not rows:
        return b"[]\n"
    parts = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows]
    return ("[\n" + ",\n".join(parts) + "\n]\n").encode("utf-8")


# ---------------------------------------------------------------------------------------------------------------
# destination safety
# ---------------------------------------------------------------------------------------------------------------

def git_tree_above(dest):
    """Return the first folder at or above dest that is a git working tree (has .git), else None."""
    p = pathlib.Path(os.path.realpath(str(dest)))
    for cand in [p] + list(p.parents):
        if (cand / ".git").exists():
            return cand
    return None


def check_dest_allowed(dest):
    """Refuse to put customer data in or under any git repo, ~/lighthouse-inspection or ~/business-context."""
    real = pathlib.Path(os.path.realpath(str(dest)))
    repo = git_tree_above(real)
    if repo is not None:
        raise BackupError("Refusing to run: %s is inside a git folder (%s). The backup holds customer details, so it "
                          "must live outside every git repo. Use a different --dest." % (real, repo))
    banned = [REPO_ROOT, HOME / "lighthouse-inspection", HOME / "business-context"]
    for b in banned:
        b = pathlib.Path(os.path.realpath(str(b)))
        if real == b or b in real.parents:
            raise BackupError("Refusing to run: %s is inside %s. Backups with customer details must not go there. "
                              "Use a different --dest." % (real, b))
    return real


def ensure_private_dir(path):
    """Make the folder (and any missing parents) private to this user (700)."""
    path = pathlib.Path(path)
    missing = []
    p = path
    while not p.exists() and p.parent != p:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        os.mkdir(str(d), 0o700)
        try:
            os.chmod(str(d), 0o700)
        except OSError:
            pass
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------------------------
# talking to Supabase (read-only)
# ---------------------------------------------------------------------------------------------------------------

def supabase_settings():
    txt = CFG_JS.read_text(encoding="utf-8") if CFG_JS.exists() else ""
    url = re.search(r'url:\s*"([^"]+)"', txt)
    key = re.search(r'anonKey:\s*"([^"]+)"', txt)
    if not url or not key:
        raise BackupError("Can't find the database address in lpc-config.js (%s)" % CFG_JS)
    return url.group(1).rstrip("/"), key.group(1)


def login_email():
    try:
        email = json.loads(USER_CONFIG.read_text(encoding="utf-8")).get("email")
    except (OSError, ValueError):
        email = None
    if not email:
        raise BackupError("No login email found in %s. Run inbox_setup.sh once (the backup uses the same login)." % USER_CONFIG)
    return email


def keychain_password(email):
    r = subprocess.run(["security", "find-generic-password", "-a", email, "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BackupError("No password in the Keychain yet. Run inbox_setup.sh once.")
    return r.stdout.rstrip("\n")


def short_err(body):
    """Pull a short message out of a Supabase error body, without echoing anything else."""
    try:
        j = json.loads(body)
        msg = j.get("message") or j.get("error_description") or j.get("error") or ""
        return str(msg)[:160]
    except Exception:
        return ""


class Client:
    """Read-only Supabase client. Holds the access token in memory only."""

    READ_ONLY_POST = (r"^/auth/v1/token\?grant_type=password$", r"^/storage/v1/object/list/[A-Za-z0-9_-]+$")

    def __init__(self, base, anon, email, get_password, pause=2.0):
        self.base = base
        self.anon = anon
        self.email = email
        self.get_password = get_password        # called again if the token expires (never stored)
        self.pause = pause
        self.token = None

    def sign_in(self):
        pw = self.get_password(self.email)
        try:
            st, _, body = self._send("POST", "/auth/v1/token?grant_type=password",
                                     {"apikey": self.anon, "Content-Type": "application/json"},
                                     {"email": self.email, "password": pw}, auth=False)
        finally:
            pw = None
        tok = json.loads(body).get("access_token") if body else None
        if not tok:
            raise BackupError("Login didn't give back a token")
        self.token = tok
        return tok

    def user_id(self):
        try:
            seg = self.token.split(".")[1]
            seg += "=" * (-len(seg) % 4)
            return json.loads(base64.urlsafe_b64decode(seg.encode())).get("sub")
        except Exception:
            return None

    def _allowed(self, method, path):
        if method == "GET":
            return True
        if method == "POST":
            return any(re.match(p, path) for p in self.READ_ONLY_POST)
        return False

    def _label(self, path):
        p = path.split("?")[0]
        return p[:70]

    def _send(self, method, path, headers, body=None, auth=True, stream_to=None):
        """One request with retries. Returns (status, headers, body bytes). With stream_to, the body goes to that
        open file and the value returned is (status, headers, (size, sha256))."""
        if not self._allowed(method, path):
            raise BackupError("Blocked: this script is read-only and will not send %s %s" % (method, self._label(path)))
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        last = "unknown error"
        for attempt in range(4):
            hdrs = dict(headers)
            if auth:
                hdrs["Authorization"] = "Bearer " + self.token
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    if stream_to is None:
                        return r.status, r.headers, r.read()
                    stream_to.seek(0)
                    stream_to.truncate()
                    h = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        stream_to.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
                    return r.status, r.headers, (size, h.hexdigest())
            except urllib.error.HTTPError as e:
                try:
                    eb = e.read()
                except Exception:
                    eb = b""
                if e.code == 401 and auth and not refreshed:
                    refreshed = True
                    self.sign_in()
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    last = "HTTP %s" % e.code
                    time.sleep(self.pause * (attempt + 1))
                    continue
                msg = short_err(eb)
                err = BackupError("HTTP %s from Supabase (%s)%s" % (e.code, self._label(path), (": " + msg) if msg else ""))
                err.code = e.code
                raise err
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                reason = getattr(e, "reason", e)
                last = "No connection (%s)" % str(reason)[:80]
                if attempt < 3:
                    time.sleep(self.pause * (attempt + 1))
                    continue
        raise BackupError("%s (%s)" % (last, self._label(path)))

    def get(self, path, extra=None):
        h = {"apikey": self.anon}
        if extra:
            h.update(extra)
        return self._send("GET", path, h)

    def post_list(self, bucket, body):
        path = "/storage/v1/object/list/" + urllib.parse.quote(bucket, safe="")
        st, _, b = self._send("POST", path, {"apikey": self.anon, "Content-Type": "application/json"}, body)
        return json.loads(b)

    def download(self, bucket, obj_path, fileobj):
        path = "/storage/v1/object/authenticated/%s/%s" % (urllib.parse.quote(bucket, safe=""),
                                                           urllib.parse.quote(obj_path))
        st, _, res = self._send("GET", path, {"apikey": self.anon}, stream_to=fileobj)
        return res


# ---------------------------------------------------------------------------------------------------------------
# exporting
# ---------------------------------------------------------------------------------------------------------------

def parse_total(content_range):
    """'0-999/1234' -> 1234, '*/0' -> 0, anything else -> None."""
    if not content_range or "/" not in content_range:
        return None
    t = content_range.rsplit("/", 1)[1].strip()
    return int(t) if t.isdigit() else None


def fetch_table(cl, name, order, page):
    """Return (rows, expected_total). Pages with limit/offset until the database says it has no more."""
    rows = []
    total = None
    off = 0
    while True:
        q = "/rest/v1/%s?select=*&order=%s&limit=%d&offset=%d" % (name, order, page, off)
        extra = {"Prefer": "count=exact"} if off == 0 else None
        st, hdrs, body = cl.get(q, extra)
        if off == 0:
            total = parse_total(hdrs.get("Content-Range"))
        batch = json.loads(body) if body else []
        if not isinstance(batch, list):
            raise BackupError("Unexpected answer for %s" % name)
        if not batch:
            break
        rows.extend(batch)
        off += len(batch)
        if total is not None and off >= total:
            break
        if total is None and len(batch) < page:
            break
        if off > 10000000:
            raise BackupError("Too many rows in %s, stopping" % name)
    return rows, total


def list_bucket(cl, bucket):
    """Walk the whole bucket (folders inside folders). Returns a list of object records, sorted by path."""
    out = []
    stack = [""]
    seen = set()
    while stack:
        prefix = stack.pop()
        if prefix in seen:
            continue
        seen.add(prefix)
        off = 0
        while True:
            items = cl.post_list(bucket, {"prefix": prefix, "limit": 1000, "offset": off,
                                          "sortBy": {"column": "name", "order": "asc"}})
            if not isinstance(items, list):
                raise BackupError("Unexpected answer listing %s" % bucket)
            for it in items:
                name = it.get("name") or ""
                if not name:
                    continue
                if it.get("id") is None:                      # a folder
                    stack.append(prefix + name + "/")
                    continue
                meta = it.get("metadata") or {}
                out.append({
                    "path": prefix + name,
                    "size": meta.get("size", meta.get("contentLength")),
                    "mimetype": meta.get("mimetype"),
                    "created_at": it.get("created_at"),
                    "updated_at": it.get("updated_at"),
                    "id": it.get("id"),
                    "etag": meta.get("eTag"),
                })
            if len(items) < 1000:
                break
            off += len(items)
    out.sort(key=lambda o: o["path"])
    return out


def is_missing(err):
    """A table or bucket that doesn't exist (yet), as opposed to one we're not allowed to read."""
    code = getattr(err, "code", None)
    text = str(err)
    return code == 404 or "PGRST205" in text or "Could not find the table" in text or "Bucket not found" in text


def safe_rel(path):
    """True if an object path is safe to turn into a file under photos/<bucket>/."""
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def parse_when(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def newest_backup(root, exclude=None):
    folders = list_backup_folders(root)
    folders = [f for f in folders if f.name != exclude]
    return folders[-1] if folders else None


def previous_photo_index(prev, bucket):
    """What the previous backup holds for this bucket: path -> (size, updated_at, sha256), only where the file is there."""
    if prev is None:
        return {}
    try:
        man = json.loads((prev / "manifest.json").read_text(encoding="utf-8"))
        lst = json.loads((prev / ("storage_%s.json" % bucket)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = man.get("files") or {}
    idx = {}
    for o in lst:
        rel = "photos/%s/%s" % (bucket, o.get("path"))
        meta = files.get(rel)
        if meta and (prev / rel).is_file():
            idx[o["path"]] = (o.get("size"), o.get("updated_at"), meta.get("sha256"))
    return idx


def download_photos(cl, bucket, objects, outdir, since_dt, prev, record, errors, progress_every=25):
    """Download objects into outdir/photos/<bucket>/. record(rel, sha, size) registers each file.
    A file already in the previous backup (same size and updated time, same hash) is hard linked, not downloaded."""
    prev_idx = previous_photo_index(prev, bucket)
    todo = []
    for o in objects:
        if o["path"].endswith(".emptyFolderPlaceholder"):
            continue
        if since_dt is not None:
            w = parse_when(o.get("updated_at")) or parse_when(o.get("created_at"))
            if w is not None and w < since_dt:
                continue
        todo.append(o)
    got = linked = failed = nbytes = 0
    for i, o in enumerate(todo, 1):
        path = o["path"]
        if not safe_rel(path):
            errors.append("%s: skipped an unsafe object name" % bucket)
            failed += 1
            continue
        rel = "photos/%s/%s" % (bucket, path)
        dest = outdir / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(str(dest.parent), 0o700)
        except OSError:
            pass
        done = False
        pv = prev_idx.get(path)
        if pv and pv[0] == o.get("size") and pv[1] == o.get("updated_at") and pv[2]:
            src = prev / rel
            try:
                if sha256_file(src) == pv[2]:
                    os.link(str(src), str(dest))
                    record(rel, pv[2], os.path.getsize(str(dest)))
                    linked += 1
                    nbytes += os.path.getsize(str(dest))
                    done = True
            except OSError:
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
        if not done:
            tmp = dest.with_name(dest.name + ".part")
            try:
                fd = os.open(str(tmp), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "r+b") as f:
                    size, sha = cl.download(bucket, path, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(str(tmp), str(dest))
                try:
                    os.chmod(str(dest), 0o600)
                except OSError:
                    pass
                if o.get("size") is not None and size != o["size"]:
                    errors.append("%s/%s: downloaded %d bytes but the listing says %d" % (bucket, path, size, o["size"]))
                    failed += 1
                    continue
                record(rel, sha, size)
                got += 1
                nbytes += size
            except (BackupError, OSError) as e:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                errors.append("%s/%s: %s" % (bucket, path, e))
                failed += 1
        if i % progress_every == 0:
            say("  %s: %d of %d photos..." % (bucket, i, len(todo)))
    return {"wanted": len(todo), "downloaded": got, "linked": linked, "failed": failed, "bytes": nbytes}


# ---------------------------------------------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------------------------------------------

def list_backup_folders(root):
    """Folders under root that this script made: name matches the pattern AND it holds our manifest.json."""
    root = pathlib.Path(root)
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if p.is_symlink() or not p.is_dir() or not NAME_RE.match(p.name):
            continue
        m = p / "manifest.json"
        if not m.is_file():
            continue
        try:
            if json.loads(m.read_text(encoding="utf-8")).get("script") != SCRIPT_NAME:
                continue
        except (OSError, ValueError):
            continue
        out.append(p)
    return out


def prune(root, keep, dry_run, pending_new=None):
    """Keep the newest `keep` folders. pending_new is the name of a folder a dry run says it would create."""
    folders = list_backup_folders(root)
    names = [f.name for f in folders]
    if pending_new and pending_new not in names:
        names.append(pending_new)
    names.sort()
    victims = names[:-keep] if len(names) > keep else []
    victims = [v for v in victims if v != pending_new]
    say("Retention: keeping the newest %d, %d folder(s) found%s." % (keep, len(names), " (counting the new one)" if pending_new else ""))
    for v in victims:
        target = pathlib.Path(root) / v
        if dry_run:
            say("  would remove %s" % target)
            continue
        if target.is_symlink() or not target.is_dir():
            continue
        shutil.rmtree(str(target))
        say("  removed %s" % target)
    if not victims:
        say("  nothing to remove.")
    return victims


def clean_stale_partials(root, dry_run):
    """Remove half-finished folders (marked by this script) left by an earlier crash, if older than 2 hours."""
    root = pathlib.Path(root)
    if not root.is_dir():
        return
    for p in root.iterdir():
        if p.name.startswith(PARTIAL_PREFIX) and p.is_dir() and not p.is_symlink() and (p / PARTIAL_MARKER).exists():
            if time.time() - p.stat().st_mtime > 7200:
                if dry_run:
                    say("  would remove stale half-finished folder %s" % p.name)
                else:
                    shutil.rmtree(str(p), ignore_errors=True)
                    say("  removed stale half-finished folder %s" % p.name)


# ---------------------------------------------------------------------------------------------------------------
# the backup run
# ---------------------------------------------------------------------------------------------------------------

def build_report(m):
    L = []
    a = L.append
    a("Lighthouse Pest Control: Job Calendar backup")
    a("=" * 46)
    a("Result:    %s" % m["result"])
    a("Run (Sydney): %s" % m["run_sydney"])
    a("Run (UTC):    %s" % m["run_utc"])
    a("Took:       %.0f seconds" % m["seconds"])
    a("Database:   %s" % m["supabase_host"])
    a("Script:     %s version %s" % (SCRIPT_NAME, VERSION))
    a("Folder:     %s" % m["folder"])
    a("")
    a("TABLES (%d) and rows" % len(m["tables"]))
    for name, t in m["tables"].items():
        if t["status"] == "ok":
            a("  %-20s %7d rows   %9s" % (name, t["rows"], human(t["bytes"])))
        elif t["status"] == "missing":
            a("  %-20s NOT IN THE DATABASE (%s)" % (name, t["error"]))
        else:
            a("  %-20s COULD NOT READ (%s)" % (name, t["error"]))
    a("  %-20s %7d rows" % ("TOTAL", m["total_rows"]))
    a("")
    a("PHOTO STORAGE")
    for b, s in m["storage"].items():
        if s["status"] == "ok":
            a("  %-18s %5d files, %s in the database's storage" % (b, s["objects"], human(s["bytes"])))
        elif s["status"] == "missing":
            a("  %-18s bucket not found (%s)" % (b, s["error"]))
        else:
            a("  %-18s COULD NOT LIST (%s)" % (b, s["error"]))
        if m["options"]["photos"] and s.get("download"):
            d = s["download"]
            a("    downloaded %d, reused from last backup %d, failed %d (%s)" % (d["downloaded"], d["linked"], d["failed"], human(d["bytes"])))
    if not m["options"]["photos"]:
        a("  Photo files were NOT downloaded this run (only the lists). Use --photos for the files.")
    elif m["options"]["since_days"]:
        a("  Photo files: only those added or changed in the last %s day(s)." % m["options"]["since_days"])
    a("")
    a("SIZE: about %s in %d files" % (human(m["total_bytes"]), len(m["files"]) + 2))
    a("")
    if m["errors"]:
        a("PROBLEMS (%d)" % len(m["errors"]))
        for e in m["errors"][:40]:
            a("  - " + e)
        if len(m["errors"]) > 40:
            a("  ...and %d more (see manifest.json)" % (len(m["errors"]) - 40))
    else:
        a("PROBLEMS: none")
    if m["warnings"]:
        a("")
        a("THINGS TO KNOW")
        for w in m["warnings"]:
            a("  - " + w)
    a("")
    a("NOT IN THIS BACKUP")
    a("  - The logins (Supabase Auth users). They can't be read with the app key. Staff ids in staff.json match them.")
    a("  - The SQL that builds the tables. That lives in ~/lighthouse-supabase (01_schema.sql onwards).")
    a("  - Any table made outside those SQL files. The table list is fixed inside the script.")
    a("")
    a("To check this folder later:   python3 backup_supabase.py --verify %s" % m["folder"])
    return "\n".join(L) + "\n"


def run_backup(args, client=None, notify_fn=notify):
    dest_root = check_dest_allowed(pathlib.Path(args.dest).expanduser())
    start = now_utc()
    syd = to_sydney(start)
    name = syd.strftime("%Y-%m-%d_%H%M")
    keep = args.keep

    if args.dry_run:
        say("DRY RUN: nothing will be signed in to, downloaded or written.")
        say("Destination root: %s" % dest_root)
        say("Would make folder: %s/%s" % (dest_root, name))
        say("Would read %d tables (all rows): %s" % (len(TABLES), ", ".join(t[0] for t in TABLES)))
        say("Would list buckets: %s" % ", ".join(BUCKETS))
        if args.photos:
            say("Would download photo files%s." % ((" added or changed in the last %d day(s)" % args.since) if args.since else " (all of them)"))
        else:
            say("Would NOT download photo files (no --photos).")
        say("Would write manifest.json and REPORT.txt, and update LAST_BACKUP.json on success.")
        clean_stale_partials(dest_root, True)
        prune(dest_root, keep, True, pending_new=name)
        return 0

    if client is None:
        base, anon = supabase_settings()
        client = Client(base, anon, login_email(), keychain_password)
    ensure_private_dir(dest_root)
    lock_fh = None
    if fcntl is not None:
        lock_fh = os.fdopen(os.open(str(dest_root / LOCK_NAME), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise BackupError("Another backup is already running. Try again in a few minutes.")
    clean_stale_partials(dest_root, False)

    final_name = name
    tick = syd
    while (dest_root / final_name).exists() or (dest_root / (PARTIAL_PREFIX + final_name)).exists():
        # a second run in the same minute: add seconds, and bump them if even that is taken
        final_name = tick.strftime("%Y-%m-%d_%H%M%S")
        tick = tick + datetime.timedelta(seconds=1)
    final = dest_root / final_name
    partial = dest_root / (PARTIAL_PREFIX + final_name)
    if partial.exists():
        shutil.rmtree(str(partial), ignore_errors=True)
    os.makedirs(str(partial), mode=0o700)
    atomic_write(partial / PARTIAL_MARKER, b"half-finished backup, safe to delete\n")
    prev_folder = newest_backup(dest_root, exclude=final_name)

    files = {}      # relative path -> {"sha256", "bytes"}
    errors = []
    warnings = []

    def record(rel, sha, size):
        files[rel] = {"sha256": sha, "bytes": size}

    def put(rel, data):
        atomic_write(partial / rel, data)
        record(rel, hashlib.sha256(data).hexdigest(), len(data))

    try:
        host = urllib.parse.urlparse(client.base).hostname or ""
        say("Signing in...")
        client.sign_in()
        my_id = client.user_id()

        tables = {}
        total_rows = 0
        t0 = time.time()
        staff_rows = None
        for tname, order, page in TABLES:
            try:
                rows, expected = fetch_table(client, tname, order, page)
                if expected is not None and len(rows) != expected:
                    # try once more (a row may have been added or removed while paging)
                    rows, expected = fetch_table(client, tname, order, page)
                if expected is not None and len(rows) != expected:
                    warnings.append("%s: the database said %d rows but %d came back (rows changed during the run?)" % (tname, expected, len(rows)))
                data = rows_to_bytes(rows)
                put(tname + ".json", data)
                tables[tname] = {"status": "ok", "rows": len(rows), "bytes": len(data), "file": tname + ".json", "error": None}
                total_rows += len(rows)
                if tname == "staff":
                    staff_rows = rows
                say("  %-18s %6d rows" % (tname, len(rows)))
            except BackupError as e:
                if is_missing(e):
                    tables[tname] = {"status": "missing", "rows": 0, "bytes": 0, "file": None, "error": str(e)}
                    warnings.append("%s: not in the database yet (nothing to back up). If you expected it, run its SQL file." % tname)
                    say("  %-18s not in the database" % tname)
                else:
                    tables[tname] = {"status": "error", "rows": 0, "bytes": 0, "file": None, "error": str(e)}
                    errors.append("table %s: %s" % (tname, e))
                    say("  %-18s ERROR %s" % (tname, e))

        # make sure we really are the master, otherwise row level security would have quietly hidden rows
        if staff_rows is not None:
            me = [s for s in staff_rows if s.get("id") == my_id]
            if not me or me[0].get("role") != "master" or not me[0].get("active"):
                errors.append("The login used is not an active master user, so the export may be missing rows. Check ~/.lpc-inbox.json and the Keychain item.")
        elif "staff" in tables and tables["staff"]["status"] != "ok":
            errors.append("Couldn't read staff, so can't confirm the login is the master user.")
        if staff_rows is not None and not staff_rows:
            errors.append("staff came back empty, which should never happen. The login probably isn't the master.")
        for must in ("clients", "jobs"):
            if tables.get(must, {}).get("status") == "ok" and tables[must]["rows"] == 0:
                warnings.append("%s has 0 rows. If that's wrong, the export is incomplete." % must)

        storage = {}
        listings = {}
        for b in BUCKETS:
            try:
                objs = list_bucket(client, b)
                put("storage_%s.json" % b, rows_to_bytes(objs))
                listings[b] = objs
                nb = sum((o.get("size") or 0) for o in objs)
                storage[b] = {"status": "ok", "objects": len(objs), "bytes": nb, "file": "storage_%s.json" % b, "error": None}
                say("  storage %-17s %5d files, %s" % (b, len(objs), human(nb)))
            except BackupError as e:
                if is_missing(e):
                    storage[b] = {"status": "missing", "objects": 0, "bytes": 0, "file": None, "error": str(e)}
                    warnings.append("bucket %s not found." % b)
                else:
                    storage[b] = {"status": "error", "objects": 0, "bytes": 0, "file": None, "error": str(e)}
                    errors.append("storage %s: %s" % (b, e))
                say("  storage %-17s ERROR %s" % (b, e))

        if args.photos:
            since_dt = (start - datetime.timedelta(days=args.since)) if args.since else None
            for b, objs in listings.items():
                say("Downloading photos from %s (%d listed)..." % (b, len(objs)))
                perr = []
                storage[b]["download"] = download_photos(client, b, objs, partial, since_dt, prev_folder, record, perr)
                for e in perr:
                    errors.append("photo " + e)
                d = storage[b]["download"]
                say("  %s: %d downloaded, %d reused, %d failed" % (b, d["downloaded"], d["linked"], d["failed"]))

        result = "OK" if not errors else "PARTIAL (see PROBLEMS)"
        finish = now_utc()
        manifest = {
            "script": SCRIPT_NAME,
            "script_version": VERSION,
            "ok": not errors,
            "result": result,
            "run_sydney": to_sydney(start).isoformat(timespec="seconds"),
            "run_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seconds": round((finish - start).total_seconds(), 1),
            "supabase_host": host,
            "folder": str(final),
            "options": {"photos": bool(args.photos), "since_days": args.since},
            "tables": tables,
            "total_rows": total_rows,
            "storage": storage,
            "errors": errors,
            "warnings": warnings,
            "files": files,
        }
        manifest["total_bytes"] = sum(f["bytes"] for f in files.values())
        report = build_report(manifest).encode("utf-8")
        put("REPORT.txt", report)
        manifest["files"] = files
        manifest["total_bytes"] = sum(f["bytes"] for f in files.values())
        mdata = (json.dumps(manifest, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        atomic_write(partial / "manifest.json", mdata)
        manifest_sha = hashlib.sha256(mdata).hexdigest()
        try:
            (partial / PARTIAL_MARKER).unlink()
        except OSError:
            pass
        os.replace(str(partial), str(final))
        try:
            os.chmod(str(final), 0o700)
        except OSError:
            pass
    except BaseException:
        shutil.rmtree(str(partial), ignore_errors=True)
        raise
    finally:
        if lock_fh is not None:
            try:
                lock_fh.close()
            except OSError:
                pass

    ok_tables = sum(1 for t in tables.values() if t["status"] == "ok")
    say("")
    say("%s  %d tables, %d rows, %s, %.0f seconds" % (manifest["result"], ok_tables, total_rows, human(manifest["total_bytes"]), manifest["seconds"]))
    say("Folder: %s" % final)
    for w in warnings:
        say("Note: " + w)
    for e in errors[:20]:
        say("PROBLEM: " + e)

    if not errors:
        last = {"when": manifest["run_sydney"], "when_utc": manifest["run_utc"], "tables": ok_tables, "rows": total_rows,
                "ok": True, "folder": str(final), "photos": bool(args.photos), "bytes": manifest["total_bytes"],
                "manifest_sha256": manifest_sha, "script_version": VERSION}
        atomic_write(dest_root / "LAST_BACKUP.json", (json.dumps(last, indent=1) + "\n").encode("utf-8"))
        prune(dest_root, keep, False)
        return 0
    notify_fn("Job Calendar backup had problems", "%d problem(s). See REPORT.txt in %s" % (len(errors), final.name))
    say("Not updating LAST_BACKUP.json and not removing old backups because of the problems above.")
    return 2


# ---------------------------------------------------------------------------------------------------------------
# verify and restore-check (no network, no Supabase)
# ---------------------------------------------------------------------------------------------------------------

def resolve_folder(arg, root):
    if arg in (None, "LATEST"):
        f = newest_backup(root)
        if f is None:
            raise BackupError("No backup folders found in %s" % root)
        return f
    p = pathlib.Path(arg).expanduser()
    if not p.is_dir() and NAME_RE.match(str(arg)) and (pathlib.Path(root) / arg).is_dir():
        p = pathlib.Path(root) / arg
    if not p.is_dir():
        raise BackupError("Not a folder: %s" % arg)
    if not (p / "manifest.json").is_file():
        raise BackupError("No manifest.json in %s (is it a backup folder?)" % p)
    return p


def verify_folder(folder):
    folder = pathlib.Path(folder)
    try:
        man = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BackupError("Can't read manifest.json: %s" % e)
    files = man.get("files") or {}
    bad = []
    broken = set()
    for rel, meta in sorted(files.items()):
        p = folder / rel
        if not p.is_file():
            bad.append("MISSING   %s" % rel)
            broken.add(rel)
            continue
        size = p.stat().st_size
        if size != meta.get("bytes"):
            bad.append("SIZE      %s (manifest %s bytes, file %s bytes)" % (rel, meta.get("bytes"), size))
            broken.add(rel)
            continue
        if sha256_file(p) != meta.get("sha256"):
            bad.append("CHANGED   %s (hash does not match the manifest)" % rel)
            broken.add(rel)
    known = set(files) | {"manifest.json"}
    for p in folder.rglob("*"):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(folder).as_posix()
            if rel not in known:
                bad.append("EXTRA     %s (not in the manifest)" % rel)
    # row counts: each table file must still hold the number of rows the manifest says
    for tname, t in (man.get("tables") or {}).items():
        fname = t.get("file")
        if t.get("status") != "ok" or not fname or fname in broken:
            continue
        try:
            n = len(json.loads((folder / fname).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            bad.append("UNREADABLE %s" % fname)
            continue
        if n != t.get("rows"):
            bad.append("ROWS      %s has %d rows, manifest says %s" % (fname, n, t.get("rows")))
    say("Verifying %s (backup of %s, script %s)" % (folder, man.get("run_sydney", "?"), man.get("script_version", "?")))
    if bad:
        say("PROBLEMS (%d):" % len(bad))
        for b in bad:
            say("  " + b)
        say("NOT OK")
        return 1
    say("OK: %d files match the manifest, row counts agree." % len(files))
    if not man.get("ok", True):
        say("Note: this backup finished PARTIAL (some tables or photos could not be read). See REPORT.txt.")
    return 0


def restore_check(folder):
    folder = pathlib.Path(folder)
    try:
        man = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BackupError("Can't read manifest.json: %s" % e)
    say("Restore check on %s (nothing is written anywhere)" % folder)
    problems = 0
    data = {}
    parsed = 0
    jfiles = sorted(p for p in folder.glob("*.json") if p.name != "manifest.json")
    for p in jfiles:
        try:
            data[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            parsed += 1
        except ValueError as e:
            say("  CANNOT PARSE %s (%s)" % (p.name, e))
            problems += 1
    say("JSON files that parse: %d of %d" % (parsed, len(jfiles)))
    for tname, t in (man.get("tables") or {}).items():
        if t.get("status") == "ok" and tname not in data:
            say("  MISSING table file for %s" % tname)
            problems += 1

    # duplicate primary keys (would hint that paging shifted while the export ran)
    order = {t[0]: t[1].split(",") for t in TABLES}
    for tname, rows in data.items():
        if tname not in order or not isinstance(rows, list):
            continue
        seen = set()
        dupes = 0
        for r in rows:
            k = tuple(r.get(c) for c in order[tname])
            if k in seen:
                dupes += 1
            seen.add(k)
        if dupes:
            say("  DUPLICATE keys in %s: %d" % (tname, dupes))
            problems += 1

    def check(links, hard):
        nonlocal problems
        for child, ccol, parent, pcol in links:
            if child not in data or parent not in data:
                say("  %-34s skipped (a table file is missing)" % ("%s.%s -> %s.%s" % (child, ccol, parent, pcol)))
                continue
            ids = set(r.get(pcol) for r in data[parent])
            checked = 0
            orphans = []
            for r in data[child]:
                v = r.get(ccol)
                if v is None or v == "":
                    continue
                checked += 1
                if v not in ids:
                    orphans.append((r.get("id") or r.get("key") or r.get("name") or "?", v))
            label = "%s.%s -> %s.%s" % (child, ccol, parent, pcol)
            say("  %-44s checked %5d, orphans %d" % (label, checked, len(orphans)))
            for rid, v in orphans[:15]:
                say("      orphan: row %s points at missing %s" % (str(rid)[:40], str(v)[:40]))
            if len(orphans) > 15:
                say("      ...and %d more" % (len(orphans) - 15))
            if orphans and hard:
                problems += 1

    say("Links that are real database rules:")
    check(HARD_LINKS, True)
    say("Links with no database rule (information only):")
    check(SOFT_LINKS, False)

    # photo rows that point at a file the storage list doesn't have
    lst = data.get("storage_job-photos")
    if "job_photos" in data and lst is not None:
        have = set(o.get("path") for o in lst)
        missing = [r for r in data["job_photos"] if r.get("path") not in have]
        say("  %-44s checked %5d, missing from storage list %d" % ("job_photos.path -> storage job-photos", len(data["job_photos"]), len(missing)))
        for r in missing[:10]:
            say("      row %s points at %s" % (str(r.get("id"))[:36], str(r.get("path"))[:60]))
    say("RESULT: %s" % ("OK" if problems == 0 else "%d PROBLEM(S) FOUND" % problems))
    return 0 if problems == 0 else 1


# ---------------------------------------------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(description="Read-only backup of the Job Calendar database onto this Mac.")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="folder that holds the dated backup folders (default %s)" % DEFAULT_DEST)
    ap.add_argument("--photos", action="store_true", help="also download the photo files (big)")
    ap.add_argument("--since", type=int, default=None, metavar="DAYS", help="with --photos: only photos added or changed in the last DAYS days")
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT, help="how many backup folders to keep (default %d)" % KEEP_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing, don't sign in")
    ap.add_argument("--prune-only", action="store_true", help="only remove old backup folders (use with --dry-run to preview)")
    ap.add_argument("--verify", nargs="?", const="LATEST", default=None, metavar="FOLDER", help="re-check a backup folder against its manifest (default: newest)")
    ap.add_argument("--restore-check", nargs="?", const="LATEST", default=None, metavar="FOLDER", help="check a backup parses and its links resolve (default: newest)")
    ap.add_argument("--no-notify", action="store_true", help="no Mac notification on failure")
    args = ap.parse_args(argv)
    if args.since is not None and not args.photos:
        ap.error("--since only makes sense together with --photos")
    if args.since is not None and args.since < 1:
        ap.error("--since needs 1 or more days")
    if args.keep < 1:
        ap.error("--keep needs 1 or more")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    no_popup = args.no_notify or sys.stdout.isatty()
    try:
        if args.verify is not None or args.restore_check is not None:
            root = pathlib.Path(args.dest).expanduser()
            rc = 0
            if args.verify is not None:
                rc = max(rc, verify_folder(resolve_folder(args.verify, root)))
            if args.restore_check is not None:
                if args.verify is not None:
                    say("")
                rc = max(rc, restore_check(resolve_folder(args.restore_check, root)))
            return rc
        if args.prune_only:
            root = check_dest_allowed(pathlib.Path(args.dest).expanduser())
            clean_stale_partials(root, args.dry_run)
            prune(root, args.keep, args.dry_run)
            return 0
        return run_backup(args, notify_fn=(lambda *a: None) if no_popup else notify)
    except BackupError as e:
        say("BACKUP FAILED: %s" % e)
        if not no_popup and not (args.dry_run or args.verify is not None or args.restore_check is not None or args.prune_only):
            notify("Job Calendar backup failed", str(e)[:120])
        return 1
    except KeyboardInterrupt:
        say("Stopped.")
        return 1
    except Exception as e:  # anything unexpected: say so plainly, never a stack trace with headers in it
        say("BACKUP FAILED (unexpected): %s: %s" % (type(e).__name__, str(e)[:200]))
        if not no_popup:
            notify("Job Calendar backup failed", "Unexpected error. Run it in Terminal to see.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

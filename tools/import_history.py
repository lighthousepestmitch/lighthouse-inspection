#!/usr/bin/env python3
"""Build the Job Book's message history: every text and email Mitch sent to, or got from, each client, read off this Mac.

Run on the Mac, read-only on Messages and Mail (nothing is sent, nothing in Messages or Mail is changed). No customer data
is in this file; the output is private and stays on the Mac until Mitch taps Upload from this Mac.

  python3 ~/lighthouse-inspection/tools/import_history.py                 # snapshot chat.db, read everything new, write the files
  python3 ~/lighthouse-inspection/tools/import_history.py --dry-run       # counts only, writes nothing
  python3 ~/lighthouse-inspection/tools/import_history.py --snapshot-only # just copy chat.db into the backup folder
  python3 ~/lighthouse-inspection/tools/import_history.py --since 2026-10-01
  python3 ~/lighthouse-inspection/tools/import_history.py --rescan        # ignore the cursor, read everything again

Step 1 copies chat.db (SQLite backup, so the -wal and -shm are folded in) to ~/Backups/chatdb-snapshots/<date>/chat.db
(folders 700, file 600, newest 5 kept) so texts Mitch deletes by hand are never lost. Everything is then read from that copy.
Step 2 matches senders and recipients to Job Book clients by mobile number or email (clients_*.json). A number or email that two
clients share is attached to neither (and counted). Group chats are skipped.
Step 3 writes, under jobbook-data/ (gitignored, private):
  history_N.json    {"rows": {"<clientId>": [entry, ...]}}   newest last, at most 200 per client, each file about 280 KB
  history_index.json {"made","shards","rows": {"<clientId>": {"s","lo","lc","lv","li","no","ni"}}}
  nudges.json       {"made","months","rows": {"<clientId>": {"lo","ch","due"}}}   last message of ours has no reply yet
  stoplist.json     {"made","rows": {"<clientId>": {"r": reason, "d": date}}}     the stop list, matched to clients
  entry = {"t": ISO time with offset, "dir": "out"|"in", "ch": "text"|"email"|"legacy", "via": Mitch's number or address,
           "with": last 3 digits of their number or their email domain, "subj": "", "txt": full text out, first 300 chars in}
  plus, only when they apply: "rv":1 (a Google review ask), "pt":1 (email: Mail only kept the start, txt is its preview line),
  "src" and "how" (legacy entries: where the record came from, and text or email when it is known).
A cursor in ~/Backups/chatdb-snapshots/cursor.json (outside the repo) means a re-run only adds new items (a --since run leaves it alone).
If the clients' numbers or emails change the cursor is ignored once and everything is read again. Prints counts only.
"""
import argparse, calendar, collections, csv, datetime, hashlib, html, json, os, re, shutil, sqlite3, glob
import email as emaillib
from email import policy
from urllib.parse import unquote
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "jobbook-data"))
BACKUPS = os.path.expanduser("~/Backups/chatdb-snapshots")
CHAT = os.path.expanduser("~/Library/Messages/chat.db")
MAIL_INDEX_GLOB = os.path.expanduser("~/Library/Mail/V*/MailData/Envelope Index")
REPLYDESK = os.path.expanduser("~/Dashboard/replydesk-log.jsonl")
STOP_CSV = os.path.expanduser("~/Documents/Lighthouse Quarterly/stop_list.csv")

TZ = ZoneInfo("Australia/Sydney")        # Skinners Shoot NSW
APPLE = 978307200                        # seconds from 1970 to 2001, Messages counts from 2001
KEEP_SNAPSHOTS = 5
MAX_PER_CLIENT = 200
SHARD_BYTES = 280000
NUDGE_MONTHS = 3
IN_CHARS = 300
OUT_CHARS = 4000
MY_MAIL_FALLBACK = "mitch@lighthousepest.com.au"
ASK = re.compile(r"g\.page/r/|google review|review on google", re.I)          # same test as import_contacted.py
REVIEW_EMAIL_SUBJECT = "Thanks from Lighthouse Pest Control"
BOUNCE_FROM = re.compile(r"^(mailer-daemon|postmaster)@|no-?reply@", re.I)
BOUNCE_SUBJ = re.compile(r"undeliver|delivery status|delivery failure|returned mail|automatic reply|out of office", re.I)


# ---------- small helpers ----------
def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if d.startswith("61"): d = "0" + d[2:]
    elif len(d) == 9 and d[0] in "2345678": d = "0" + d       # 412345678 -> 0412345678
    return d[-10:] if len(d) >= 9 else ""

def norm_mail(a):
    a = (a or "").strip().lower()
    return a if re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", a) else ""

def split_phones(s): return [norm_phone(x) for x in re.split(r"[,/;]| or ", s or "") if norm_phone(x)]
def split_mails(s): return [norm_mail(x) for x in re.split(r"[,;/\s]+", s or "") if norm_mail(x)]

def iso_local(ts): return datetime.datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")
def day_local(ts): return datetime.datetime.fromtimestamp(ts, TZ).date().isoformat()

def epoch_of(t):
    """ISO string (with offset) or a bare date -> epoch seconds"""
    try:
        if len(t) == 10: return datetime.datetime.strptime(t, "%Y-%m-%d").replace(tzinfo=TZ).timestamp()
        return datetime.datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0

def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12); y += d.year; m += 1
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))

def with_phone(n): return n[-3:] if n else ""
def with_mail(a): return a.split("@")[-1] if "@" in a else ""

def chmod(path, mode):
    try: os.chmod(path, mode)
    except OSError: pass

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    chmod(tmp, 0o600); os.replace(tmp, path)


# ---------- step 1: snapshot ----------
def snapshot(chat_path, root):
    os.makedirs(root, exist_ok=True); chmod(root, 0o700)
    dest = os.path.join(root, datetime.date.today().isoformat())
    os.makedirs(dest, exist_ok=True); chmod(dest, 0o700)
    out = os.path.join(dest, "chat.db")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(out + ext): os.remove(out + ext)
    src = sqlite3.connect(f"file:{chat_path}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)                                  # a consistent copy with the -wal folded in
        dst.execute("pragma journal_mode=delete")        # one self-contained file
        dst.commit()
    finally:
        dst.close(); src.close()
    for ext in ("-wal", "-shm"):
        if os.path.exists(out + ext): os.remove(out + ext)
    chmod(out, 0o600)
    days = sorted(d for d in os.listdir(root) if re.match(r"^\d{4}-\d{2}-\d{2}$", d) and os.path.isdir(os.path.join(root, d)))
    for old in days[:-KEEP_SNAPSHOTS]: shutil.rmtree(os.path.join(root, old), ignore_errors=True)
    return out


# ---------- clients and matching ----------
def load_clients(data):
    clients = {}
    for f in sorted(glob.glob(os.path.join(data, "clients_*.json"))):
        for cid, c in json.load(open(f))["rows"].items(): clients[cid] = c
    return clients

def build_maps(clients, mine_phones, mine_mails):
    ph, em = {}, {}
    for cid, c in clients.items():
        for n in split_phones(c.get("p")):
            if n not in mine_phones: ph.setdefault(n, set()).add(cid)
        for a in split_mails(c.get("e")):
            if a not in mine_mails and not a.endswith("@lighthousepest.com.au"): em.setdefault(a, set()).add(cid)
    uph = {k: next(iter(v)) for k, v in ph.items() if len(v) == 1}
    uem = {k: next(iter(v)) for k, v in em.items() if len(v) == 1}
    shared = {"phones": sum(1 for v in ph.values() if len(v) > 1), "emails": sum(1 for v in em.values() if len(v) > 1)}
    sig = hashlib.sha1(json.dumps([sorted(uph.items()), sorted(uem.items())]).encode()).hexdigest()
    return uph, uem, ph, em, shared, sig


# ---------- step 2a: texts ----------
def ab_text(ab):
    """the words out of a Messages attributedBody blob (typedstream): after NSString comes \\x01\\x94\\x84\\x01+ then a length"""
    if not ab: return ""
    i = ab.find(b"NSString")
    if i < 0: return ""
    j = ab.find(b"\x84\x01+", i)
    if j < 0: return ""
    p = j + 3; n = ab[p]
    if n == 0x81: n = int.from_bytes(ab[p + 1:p + 3], "little"); p += 3
    elif n == 0x82: n = int.from_bytes(ab[p + 1:p + 5], "little"); p += 5
    else: p += 1
    return ab[p:p + n].decode("utf-8", "ignore")

def msg_words(text, ab, has_att, audio):
    t = (text or ab_text(ab) or "").replace("￼", "").strip()
    if t: return t
    if audio: return "[voice message]"
    if has_att: return "[photo or file]"
    return ""

def read_chat(path, uph, uem, since_rowid, since_ts, stats):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # one row per message, with the other person when the chat has exactly one (a group chat has several: skipped)
    rows = db.execute("""
      select m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody, m.destination_caller_id, m.cache_has_attachments,
             m.is_audio_message, coalesce(m.associated_message_type, 0), coalesce(m.item_type, 0), coalesce(m.error, 0),
             (select count(*) from chat_handle_join c2 where c2.chat_id = cmj.chat_id),
             (select h.id from chat_handle_join c3 join handle h on h.ROWID = c3.handle_id where c3.chat_id = cmj.chat_id limit 1)
      from message m left join chat_message_join cmj on cmj.message_id = m.ROWID
      where m.ROWID > ? group by m.ROWID order by m.ROWID""", (since_rowid,)).fetchall()
    out, top = [], since_rowid
    for rid, d, fromme, text, ab, dest, att, audio, assoc, item, err, nh, hid in rows:
        top = max(top, rid)
        ts = (d / 1e9 if d > 1e12 else d) + APPLE
        if since_ts and ts < since_ts: continue
        stats["chat_read"] += 1
        if assoc: stats["chat_skip_tapback"] += 1; continue
        if item: stats["chat_skip_system"] += 1; continue
        if fromme and err: stats["chat_skip_failed"] += 1; continue
        if nh != 1 or not hid: stats["chat_skip_group_or_nochat"] += 1; continue
        txt = msg_words(text, ab, att, audio)
        if not txt: stats["chat_skip_empty"] += 1; continue
        if "@" in hid: other, cid = hid.lower(), uem.get(hid.lower()); w = with_mail(other)
        else: other = norm_phone(hid); cid = uph.get(other); w = with_phone(other)
        if not cid: stats["chat_skip_nomatch"] += 1; continue
        dd = (dest or "").replace("tel:", "")
        via = (norm_phone(dd) or dd.lower()) if dd else ""
        e = {"ts": ts, "dir": "out" if fromme else "in", "ch": "text", "via": via, "with": w, "subj": "",
             "txt": txt if fromme else txt[:IN_CHARS]}
        if fromme and ASK.search(txt): e["rv"] = 1
        stats["chat_out" if fromme else "chat_in"] += 1
        out.append((cid, e))
    db.close()
    return out, top


# ---------- step 2b: email ----------
def html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h))

def emlx_text(path):
    try:
        with open(path, "rb") as f:
            n = int(f.readline().strip()); raw = f.read(n)
        msg = emaillib.message_from_bytes(raw, policy=policy.default)
        for pref in (("plain",), ("html",)):
            part = msg.get_body(preferencelist=pref)
            if part is None: continue
            try: s = part.get_content()
            except Exception: continue
            if pref == ("html",): s = html_to_text(s)
            if s and s.strip(): return s
    except Exception:
        return ""
    return ""

QUOTE_HDR = re.compile(r"^\s*(on .{3,200}wrote:|-{2,}\s*(original message|forwarded message).*|begin forwarded message:|_{8,}|\*{0,2}from:\*{0,2}\s.+)\s*$", re.I)
def cut_quoted(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").split("\n")
    keep = []
    for i, ln in enumerate(lines):
        if ln.startswith(">") or re.match(r"^\s*sent from my (iphone|ipad)", ln, re.I): break
        two = ln + " " + (lines[i + 1] if i + 1 < len(lines) else "")         # "On Mon ... \n ... wrote:" wraps onto a second line
        if QUOTE_HDR.match(ln) or (re.match(r"^\s*on .{3,200}$", ln, re.I) and re.search(r"wrote:\s*$", two, re.I)): break
        keep.append(ln.rstrip())
    if not "".join(keep).strip():               # all quoted (a forward, or a reply typed under the quote): keep it, without the > marks
        keep = [re.sub(r"^(\s*>)+ ?", "", ln).rstrip() for ln in lines]
    return keep

SIGNISH = re.compile(r"lighthouse|licen[cs]e|abn|www\.|https?:|\.com|\.au\b|@|\bph\b|phone|mobile|\btel\b|mob[: ]|skinners|\bnsw\b|\[|cid:|^--|^\+?[\d\s()\-]{8,}$|director|mitch(ell)? begg|sent from my|get outlook", re.I)
SIGNOFF = re.compile(r"^(cheers|thanks|thank you|regards|kind regards|many thanks|ta|best|warm regards)\b|^mitch$", re.I)
def norm_line(s): return re.sub(r"\s+", " ", s.strip().lower())

def learn_sig(bodies, min_emails=25):
    from collections import Counter
    c = Counter()
    for lines in bodies:
        for s in {norm_line(x) for x in lines[-14:] if x.strip()}:
            if SIGNISH.search(s) and not SIGNOFF.search(s): c[s] += 1
    return {s for s, n in c.items() if n >= min_emails}

def strip_sig(lines, sig):
    lines = list(lines)
    while lines and (not lines[-1].strip() or norm_line(lines[-1]) in sig or re.match(r"^\s*(sent from my|get outlook)", lines[-1], re.I)): lines.pop()
    return "\n".join(lines)

def tidy(s, limit):
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:limit]

def mail_folder_kind(url):
    u = unquote(url)
    tail = u.split("/", 3)[-1] if "://" in u else u
    parts = tail.split("/")
    folder = "/".join(parts)
    last = parts[-1] if parts else ""
    if last in ("Sent", "Sent Messages"): return "out"
    if folder in ("INBOX", "INBOX/Archive") or last in ("INBOX", "Archive"): return "in"
    return None

def read_mail(index_path, uem, since_ts, stats, sig_known):
    db = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    boxes = {}
    for rid, url in db.execute("select ROWID, url from mailboxes"):
        k = mail_folder_kind(url)
        if k: boxes[rid] = k
    if not boxes: return [], 0, since_ts or 0, set(sig_known)
    ids = ",".join(str(b) for b in boxes)
    outs = ",".join(str(b) for b, k in boxes.items() if k == "out") or "0"
    mine = {a for (a,) in db.execute(f"""select distinct lower(ad.address) from messages m join addresses ad on ad.ROWID = m.sender
        where m.mailbox in ({outs}) and m.deleted = 0""")}                       # the addresses Mitch sends from
    # where the Sent bodies live: <Mail>/V*/<account>/**/Sent*.mbox/**/Messages/<ROWID>.emlx
    root = os.path.dirname(os.path.dirname(index_path))        # ~/Library/Mail/V10
    files = {}
    for dp, dn, fn in os.walk(root):
        if "Sent" not in dp: continue
        for f in fn:
            m = re.match(r"(\d+)\.(partial\.)?emlx$", f)
            if m: files[int(m.group(1))] = os.path.join(dp, f)
    rcpt = {}
    for mid, a, typ in db.execute(f"""select r.message, lower(ad.address), r.type from recipients r join addresses ad on ad.ROWID = r.address
        join messages m on m.ROWID = r.message where m.mailbox in ({ids}) and m.deleted = 0 and r.type in (0, 1)"""):
        rcpt.setdefault(mid, []).append(a)
    cand = db.execute(f"""select m.ROWID, m.mailbox, m.message_id, coalesce(nullif(m.date_sent, 0), m.date_received), m.date_received,
        lower(ad.address), coalesce(sj.subject, ''), coalesce(sm.summary, '')
        from messages m join addresses ad on ad.ROWID = m.sender left join subjects sj on sj.ROWID = m.subject
        left join summaries sm on sm.ROWID = m.summary
        where m.mailbox in ({ids}) and m.deleted = 0 and m.date_received > ?""", (since_ts or 0,)).fetchall()
    top = max([r[4] for r in cand] + [since_ts or 0])
    cand.sort(key=lambda r: (r[0] not in files, r[0]))                              # a copy with a body file wins the duplicate
    seen, raw_out, raw_in = set(), [], []
    for rid, box, mid, ts, rec, sender, subj, summ in cand:
        key = ("id", mid) if mid else ("k", rec, subj, sender)
        if key in seen: stats["mail_dupes"] += 1; continue
        seen.add(key)
        kind = boxes[box]
        stats["mail_read"] += 1
        if kind == "out":
            if subj.strip() == REVIEW_EMAIL_SUBJECT: rv = 1
            else: rv = 0
            for a in rcpt.get(rid, []):
                cid = uem.get(a)
                if not cid: continue
                raw_out.append({"cid": cid, "ts": ts or rec, "via": sender, "with": with_mail(a), "subj": subj, "rv": rv, "rid": rid, "path": files.get(rid),
                                "summ": re.sub(r"\s+", " ", summ).strip()})
        else:
            if BOUNCE_FROM.search(sender) or BOUNCE_SUBJ.search(subj): stats["mail_skip_bounce"] += 1; continue
            cid = uem.get(sender)
            if not cid: continue
            via = next((a for a in rcpt.get(rid, []) if a in mine), None) or MY_MAIL_FALLBACK
            raw_in.append((cid, {"ts": ts or rec, "dir": "in", "ch": "email", "via": via, "with": with_mail(sender), "subj": subj,
                                 "txt": re.sub(r"\s+", " ", summ).strip()[:IN_CHARS]}))
    # bodies of the sent mail: quotes cut now, signatures after we know which lines repeat
    bodies = []
    for r in raw_out:
        r["lines"] = cut_quoted(emlx_text(r["path"])) if r["path"] else None
        if r["lines"] is not None: bodies.append(r["lines"])
    sig = set(sig_known) | learn_sig(bodies, 25 if len(bodies) >= 200 else 10**9)
    out = []
    for r in raw_out:
        if r["lines"] is None: txt = ""; stats["mail_out_nobody"] += 1; stats["mail_out_nofile"] += 1
        else:
            txt = tidy(strip_sig(r["lines"], sig), OUT_CHARS)
            stats["mail_out_body" if txt else "mail_out_nobody"] += 1
        e = {"ts": r["ts"], "dir": "out", "ch": "email", "via": r["via"], "with": r["with"], "subj": r["subj"], "txt": txt}
        if not txt and r["summ"]:                                   # Mail only kept the start of this one (attachments): use its preview line
            e["txt"] = r["summ"][:OUT_CHARS]; e["pt"] = 1; stats["mail_out_preview"] += 1
        if r["rv"]: e["rv"] = 1
        out.append((r["cid"], e)); stats["mail_out"] += 1
    stats["mail_in"] += len(raw_in)
    db.close()
    return out + raw_in, len(sig), top, sig


# ---------- step 2c: the older records ----------
def read_legacy(data, replydesk, uph, uem, stats):
    """(client, entry) pairs from the Dashboard send log and the older contacted/review/campaign files. Date-only ones use T = the date."""
    out = []
    def jf(name):
        try: return json.load(open(os.path.join(data, name)))
        except Exception: return None
    try:
        for ln in open(replydesk):
            try: r = json.loads(ln)
            except Exception: continue
            if not r.get("ok"): continue
            to = r.get("to") or ""
            cid = uem.get(norm_mail(to)) if "@" in to else uph.get(norm_phone(to))
            if not cid: stats["legacy_skip_nomatch"] += 1; continue
            how = "email" if r.get("channel") == "email" else "text"
            out.append((cid, {"ts": float(r.get("ts") or 0), "dir": "out", "ch": "legacy", "via": "", "with": "", "subj": r.get("subject") or "",
                              "txt": (r.get("body") or "")[:OUT_CHARS], "src": "dashboard", "how": how, "p": 3}))
    except FileNotFoundError:
        pass
    camp = (jf("campaign.json") or {}).get("rows", {})
    for cid, c in camp.items():
        for k, how in (("e", "email"), ("t", "text")):
            if c.get(k): out.append((cid, {"ts": epoch_of(c[k]) + 43200, "dir": "out", "ch": "legacy", "via": "", "with": "", "subj": "", "txt": "",
                                           "src": "campaign", "how": how, "p": 2}))
        if c.get("r"):
            last = max([epoch_of(c[k]) for k in ("e", "t") if c.get(k)] or [0])
            if last: out.append((cid, {"ts": last + 43200, "dir": "in", "ch": "legacy", "via": "", "with": "", "subj": "",
                                       "txt": "Replied (campaign record, exact day not kept)", "src": "campaign",
                                       "how": c.get("v") or "", "p": 2}))
    for cid, d in ((jf("review_asks.json") or {}).get("rows") or {}).items():
        out.append((cid, {"ts": epoch_of(d) + 43200, "dir": "out", "ch": "legacy", "via": "", "with": "", "subj": "", "txt": "",
                          "src": "review", "how": "text", "rv": 1, "p": 1}))
    for cid, d in ((jf("contacted.json") or {}).get("rows") or {}).items():
        out.append((cid, {"ts": epoch_of(d) + 43200, "dir": "out", "ch": "legacy", "via": "", "with": "", "subj": "", "txt": "",
                          "src": "contacted", "how": "", "p": 0}))
    return out


# ---------- step 3: merge, shard, index, nudges ----------
def to_public(e):
    o = {"t": e["t"] if "t" in e else iso_local(e["ts"]), "dir": e["dir"], "ch": e["ch"], "via": e.get("via", ""),
         "with": e.get("with", ""), "subj": e.get("subj", ""), "txt": e.get("txt", "")}
    for k in ("rv", "pt", "src", "how"):
        if e.get(k): o[k] = e[k]
    return o

def ekey(e):
    """same message = same time, direction, channel and other person, plus the subject (email) or the first words (text)"""
    return (e["t"], e["dir"], e["ch"], e.get("with", ""), e.get("subj", "") if e["ch"] == "email" else (e.get("txt") or "")[:40])

def load_existing(data):
    cur = {}
    for f in sorted(glob.glob(os.path.join(data, "history_[0-9]*.json"))):
        try: rows = json.load(open(f)).get("rows", {})
        except Exception: continue
        for cid, es in rows.items(): cur.setdefault(cid, []).extend(es)
    return cur

def sort_key(e):
    """oldest first; a record that only knows the day counts as midday (a reply as just after), and on a tie ours comes before theirs"""
    ep = epoch_of(e["t"])
    if len(e["t"]) == 10: ep += 43200 + (1 if e["dir"] == "in" else 0)
    return (ep, 0 if e["dir"] == "out" else 1)

def build_index(hist):
    idx = {}
    for cid, es in hist.items():
        outs = [e for e in es if e["dir"] == "out" and not e.get("rv")]
        ins = [e for e in es if e["dir"] == "in"]
        o = {"no": sum(1 for e in es if e["dir"] == "out"), "ni": len(ins)}
        if outs:
            last = max(outs, key=lambda e: epoch_of(e["t"]))
            o["lo"] = last["t"]; o["lv"] = last.get("via", "")
            o["lc"] = (last.get("how") or "legacy") if last["ch"] == "legacy" else last["ch"]      # text, email, or legacy when unknown
        if ins: o["li"] = max(ins, key=lambda e: epoch_of(e["t"]))["t"]
        idx[cid] = o
    return idx

def replied_after(li, lo):
    """a reply counts if it is later than our last message; a reply recorded by day only (the old campaign record) counts on or after that day"""
    return epoch_of(li) > epoch_of(lo) or (len(li) == 10 and li >= lo[:10])

def build_nudges(idx, dnc, months):
    rows = {}
    for cid, o in idx.items():
        if cid in dnc or "lo" not in o: continue
        if "li" in o and replied_after(o["li"], o["lo"]): continue
        due = add_months(datetime.datetime.fromtimestamp(epoch_of(o["lo"]), TZ).date(), months)
        rows[cid] = {"lo": o["lo"], "ch": o["lc"], "due": due.isoformat()}
    return rows

def write_shards(data, hist):
    shards, cur, size = [], {}, 0
    for cid in sorted(hist):
        n = len(json.dumps(hist[cid], separators=(",", ":"), ensure_ascii=False))
        if cur and size + n > SHARD_BYTES: shards.append(cur); cur, size = {}, 0
        cur[cid] = hist[cid]; size += n
    if cur: shards.append(cur)
    for old in glob.glob(os.path.join(data, "history_[0-9]*.json")): os.remove(old)
    where = {}
    for i, s in enumerate(shards):
        write_json(os.path.join(data, f"history_{i}.json"), {"rows": s})
        for cid in s: where[cid] = i
    return len(shards), where


# ---------- stop list ----------
def read_stop(path, clients, uph, uem, ph, em, stats):
    rows = {}
    try: rd = list(csv.DictReader(open(path)))
    except FileNotFoundError: return rows
    names = {}
    for cid, c in clients.items(): names.setdefault(re.sub(r"\W+", " ", (c.get("n") or "").lower()).strip(), []).append(cid)
    for r in rd:
        e, m = norm_mail(r.get("email")), norm_phone(r.get("mobile"))
        cands = set()
        if e: cands |= em.get(e, set())
        if m: cands |= ph.get(m, set())
        many = len(cands) > 1
        if many:                                              # shared contact: only the one whose name matches
            nm = names.get(re.sub(r"\W+", " ", (r.get("name") or "").lower()).strip(), [])
            cands = {c for c in cands if c in nm}
        if len(cands) == 1:
            rows[next(iter(cands))] = {"r": (r.get("reason") or "").strip(), "d": (r.get("date") or "").strip()}
        else:
            stats["stop_ambiguous" if many else "stop_unmatched"] += 1
    stats["stop_rows"] = len(rd); stats["stop_matched"] = len(rows)
    return rows


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Build the Job Book message history (read-only on Messages and Mail).")
    ap.add_argument("--dry-run", action="store_true", help="counts only, write nothing")
    ap.add_argument("--snapshot-only", action="store_true", help="only copy chat.db into the backup folder")
    ap.add_argument("--since", help="only read items on or after this date (YYYY-MM-DD)")
    ap.add_argument("--rescan", action="store_true", help="ignore the cursor and read everything again")
    ap.add_argument("--chat", default=CHAT); ap.add_argument("--mail-index", default=None)
    ap.add_argument("--data", default=DATA); ap.add_argument("--backups", default=BACKUPS)
    ap.add_argument("--replydesk", default=REPLYDESK); ap.add_argument("--stoplist", default=STOP_CSV)
    a = ap.parse_args()
    stats = collections.defaultdict(int)

    if a.snapshot_only:
        out = snapshot(a.chat, a.backups)
        print("snapshot written:", os.path.relpath(out, os.path.expanduser("~")), f"({os.path.getsize(out) // 1024} KB), kept newest {KEEP_SNAPSHOTS}")
        return
    snap = a.chat if a.dry_run else snapshot(a.chat, a.backups)
    if not a.dry_run: print("snapshot written, kept newest", KEEP_SNAPSHOTS)

    clients = load_clients(a.data)
    cur_path = os.path.join(a.backups, "cursor.json")
    try: cursor = json.load(open(cur_path))
    except Exception: cursor = {}
    since_ts = epoch_of(a.since) if a.since else 0

    # who Mitch is (his own numbers and addresses), then the match tables
    probe = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    mine_phones = set()
    for (d,) in probe.execute("select distinct destination_caller_id from message where destination_caller_id is not null and destination_caller_id <> ''"):
        d = d.replace("tel:", "")
        if "@" not in d and norm_phone(d): mine_phones.add(norm_phone(d))
    probe.close()
    mine_mails = {MY_MAIL_FALLBACK}
    mail_path = a.mail_index or next(iter(sorted(glob.glob(MAIL_INDEX_GLOB), key=os.path.getmtime, reverse=True)), None)
    uph, uem, ph, em, shared, sig = build_maps(clients, mine_phones, mine_mails)
    full = a.rescan or a.since is not None or cursor.get("match_sig") != sig                 # new clients or numbers: read everything again
    since_rowid = 0 if full else int(cursor.get("chat_rowid", 0))
    mail_since = since_ts if full else max(since_ts, float(cursor.get("mail_ts", 0)))

    chat_new, top_rowid = read_chat(snap, uph, uem, since_rowid, since_ts, stats)
    mail_new, sig_n = [], 0
    mail_top = mail_since
    sig_lines = set(cursor.get("sig_lines", []))
    if mail_path and os.path.exists(mail_path):
        mail_new, sig_n, mail_top, sig_lines = read_mail(mail_path, uem, mail_since, stats, sig_lines)
    else:
        print("Mail's Envelope Index not found: email skipped")

    # merge with what is already on file (keeps anything Mitch later deleted from Messages or Mail)
    on_file = load_existing(a.data)
    n_before = sum(len(v) for v in on_file.values())
    hist = {cid: [e for e in es if e.get("ch") != "legacy"] for cid, es in on_file.items()}      # older records are rebuilt every run
    have = {cid: {ekey(e): e for e in es} for cid, es in hist.items()}
    for cid, e in chat_new + mail_new:
        pe = to_public(e); old = have.setdefault(cid, {}).get(ekey(pe))
        if old is not None:                                   # already on file; a re-read can fill in an email body that was missing
            if pe["ch"] == "email" and (a.rescan or len(pe["txt"]) > len(old["txt"]) or (old.get("pt") and not pe.get("pt"))):
                old["txt"] = pe["txt"]; old.pop("pt", None)
                if pe.get("pt"): old["pt"] = 1
                stats["bodies_updated"] += 1
            continue
        have[cid][ekey(pe)] = pe; hist.setdefault(cid, []).append(pe); stats["new_real"] += 1

    # older records only where Messages/Mail do not already cover that day
    day_out, day_in_max = {}, {}
    for cid, es in hist.items():
        for e in es:
            if e["dir"] == "out": day_out.setdefault(cid, set()).add(e["t"][:10])
            else: day_in_max[cid] = max(day_in_max.get(cid, ""), e["t"][:10])
    legacy = sorted(read_legacy(a.data, a.replydesk, uph, uem, stats), key=lambda x: -x[1].get("p", 0))
    for cid, e in legacy:
        d = day_local(e["ts"]) if e["ts"] > 4e8 else ""
        if not d: continue
        if e["dir"] == "out":
            if d in day_out.get(cid, set()): stats["legacy_covered"] += 1; continue
            day_out.setdefault(cid, set()).add(d)
        else:
            if day_in_max.get(cid, "") >= d: stats["legacy_covered"] += 1; continue
            day_in_max[cid] = d
        pe = to_public(e)
        if e["src"] != "dashboard": pe["t"] = d                          # only a day is known
        hist.setdefault(cid, []).append(pe); stats["legacy_added_" + e["src"]] += 1

    for cid, es in hist.items():
        es.sort(key=sort_key)
        if len(es) > MAX_PER_CLIENT: stats["clients_trimmed"] += 1; del es[:len(es) - MAX_PER_CLIENT]
    hist = {cid: es for cid, es in hist.items() if es}

    idx = build_index(hist)
    stop = read_stop(a.stoplist, clients, uph, uem, ph, em, stats)
    camp = (lambda p: json.load(open(p)).get("rows", {}) if os.path.exists(p) else {})(os.path.join(a.data, "campaign.json"))
    dnc = {cid for cid, c in camp.items() if c.get("x")} | set(stop)
    nud = build_nudges(idx, dnc, NUDGE_MONTHS)
    stats["nudge_excluded_dnc"] = sum(1 for cid, o in idx.items() if cid in dnc and "lo" in o and not ("li" in o and replied_after(o["li"], o["lo"])))

    n_entries = sum(len(v) for v in hist.values())
    if not a.dry_run:
        nshards, where = write_shards(a.data, hist)
        for cid, o in idx.items(): o["s"] = where[cid]
        now = datetime.date.today().isoformat()
        write_json(os.path.join(a.data, "history_index.json"), {"made": now, "shards": nshards, "rows": idx})
        write_json(os.path.join(a.data, "nudges.json"), {"made": now, "months": NUDGE_MONTHS, "rows": nud})
        write_json(os.path.join(a.data, "stoplist.json"), {"made": now, "rows": stop})
        cursor = {"made": datetime.datetime.now(TZ).isoformat(timespec="seconds"), "match_sig": sig, "chat_rowid": top_rowid,
                  "mail_ts": mail_top, "sig_lines": sorted(sig_lines)}
        if not a.since:                                                    # a --since run is partial, so it must not move the cursor
            os.makedirs(a.backups, exist_ok=True); write_json(cur_path, cursor)
    else:
        nshards = len(write_shards_dry(hist))

    # counts only
    print(f"{'DRY RUN. ' if a.dry_run else ''}{'full read' if full else 'incremental read'}")
    print(f"clients in the Job Book: {len(clients)}  (numbers shared by 2+ clients, attached to none: {shared['phones']}, emails: {shared['emails']})")
    print(f"texts: read {stats['chat_read']}, kept {stats['chat_out']} out + {stats['chat_in']} in;"
          f" skipped group/no chat {stats['chat_skip_group_or_nochat']}, no client {stats['chat_skip_nomatch']}, failed sends {stats['chat_skip_failed']},"
          f" reactions {stats['chat_skip_tapback']}, system {stats['chat_skip_system']}, empty {stats['chat_skip_empty']}")
    print(f"email: read {stats['mail_read']} (duplicates dropped {stats['mail_dupes']}), kept {stats['mail_out']} out + {stats['mail_in']} in;"
          f" bounces/auto-replies skipped {stats['mail_skip_bounce']}; sent bodies found {stats['mail_out_body']}, preview line only {stats['mail_out_preview']}, nothing {stats['mail_out_nobody'] - stats['mail_out_preview']} (no file on this Mac {stats['mail_out_nofile']}); signature lines learned {sig_n}")
    print(f"older records added: dashboard {stats['legacy_added_dashboard']}, campaign {stats['legacy_added_campaign']},"
          f" review asks {stats['legacy_added_review']}, contacted {stats['legacy_added_contacted']}; already covered by Messages/Mail {stats['legacy_covered']}; no client {stats['legacy_skip_nomatch']}")
    print(f"on file before {n_before}, new texts and emails added {stats['new_real']} (email bodies re-read {stats['bodies_updated']}), now {n_entries} entries for {len(hist)} clients"
          f" (clients trimmed to {MAX_PER_CLIENT}: {stats['clients_trimmed']}); in {nshards} file(s)")
    print(f"index rows {len(idx)}; nudges {len(nud)} (left out as do-not-contact {stats['nudge_excluded_dnc']}); "
          f"stop list {stats['stop_rows']} rows, matched {stats['stop_matched']}, ambiguous {stats['stop_ambiguous']}, no client {stats['stop_unmatched']}")
    if not a.dry_run:
        print("\nNow open http://127.0.0.1:8942/jobbook.html on this Mac and tap Upload from this Mac.")

def write_shards_dry(hist):
    shards, size, cur = [], 0, 0
    for cid in sorted(hist):
        n = len(json.dumps(hist[cid], separators=(",", ":"), ensure_ascii=False))
        if cur and size + n > SHARD_BYTES: shards.append(cur); size, cur = 0, 0
        size += n; cur += 1
    if cur: shards.append(cur)
    return shards

if __name__ == "__main__":
    main()

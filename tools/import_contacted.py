#!/usr/bin/env python3
"""Find the last time Mitch texted or emailed each client, from Messages and Apple Mail on this Mac, and write
jobbook-data/contacted.json so the Job Book's "Text the next N" skips anyone contacted in the last 30 days,
even when the text went out from Messages or the dashboard and not from the Job Book page.

Review asks (Google review texts and the review email) are left out, the same as the Job Book's own 30 day rule.
Read-only on Messages and Mail. Only client id and date are written. The file stays on this Mac (gitignored); it reaches the
Job Book through "Upload from this Mac" on http://127.0.0.1:8942/jobbook.html.
Run it any time:  python3 ~/lighthouse-inspection/tools/import_contacted.py
Then open the Job Book on this Mac and tap Upload from this Mac.
"""
import sqlite3, re, json, glob, os, datetime, time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "jobbook-data")
CHAT = os.path.expanduser("~/Library/Messages/chat.db")
MITCH = {"0432181689"}                                  # his own mobile
MITCH_MAIL = "mitch@lighthousepest.com.au"
DAYS = 60                                               # look back this far, the Job Book only needs 30
ASK = re.compile(r"g\.page/r/|google review|review on google", re.I)
REVIEW_EMAIL_SUBJECT = "Thanks from Lighthouse Pest Control"
APPLE = 978307200

def norm(p):
    d = re.sub(r"\D", "", p or "")
    if d.startswith("61"): d = "0" + d[2:]
    return d[-10:] if len(d) >= 9 else d

def body(text, ab):
    if text: return text
    if not ab: return ""
    s = ab.decode("utf-8", "ignore")
    m = re.search(r"NSString.{1,6}?\+(.)(.*?)\x86", s, re.S)
    return m.group(2) if m else s

def day(ts): return datetime.datetime.fromtimestamp(ts).date().isoformat()

def main():
    by_phone, by_mail = {}, {}
    for f in glob.glob(os.path.join(DATA, "clients_*.json")):
        for cid, c in json.load(open(f))["rows"].items():
            for ph in re.split(r"[,/;]| or ", c.get("p") or ""):
                n = norm(ph)
                if len(n) >= 9: by_phone.setdefault(n, cid)
            for em in re.split(r"[,;/\s]+", c.get("e") or ""):
                if "@" in em: by_mail.setdefault(em.strip().lower(), cid)
    since = time.time() - DAYS * 86400
    last, texts, mails = {}, 0, 0

    db = sqlite3.connect(f"file:{CHAT}?mode=ro", uri=True)
    rows = db.execute("""
      select m.date, m.text, m.attributedBody,
        (select group_concat(h2.id) from chat_message_join cmj join chat_handle_join chj on chj.chat_id = cmj.chat_id
           join handle h2 on h2.ROWID = chj.handle_id where cmj.message_id = m.ROWID)
      from message m where m.is_from_me = 1 and coalesce(m.associated_message_type, 0) = 0
        and (m.date / 1000000000 + ?) > ?""", (APPLE, since)).fetchall()
    for d, text, ab, handles in rows:
        if ASK.search(body(text, ab)): continue
        ts = d / 1e9 + APPLE
        for h in (handles or "").split(","):
            if "@" in h: cid = by_mail.get(h.strip().lower())
            else:
                n = norm(h)
                cid = None if (not n or n in MITCH) else by_phone.get(n)
            if cid:
                texts += 1
                if day(ts) > last.get(cid, ""): last[cid] = day(ts)

    paths = glob.glob(os.path.expanduser("~/Library/Mail/V*/MailData/Envelope Index"))
    if paths:
        mdb = sqlite3.connect(f"file:{max(paths, key=os.path.getmtime)}?mode=ro", uri=True)
        acct = mdb.execute("""select substr(b.url, 1, instr(b.url, '/INBOX') - 1) a, count(*) n from messages m
            join mailboxes b on b.ROWID = m.mailbox join addresses ad on ad.ROWID = m.sender
            where b.url like '%/INBOX/Sent%' and ad.address = ? group by a order by n desc limit 1""", (MITCH_MAIL,)).fetchone()
        if acct:
            sent = [r for r, u in mdb.execute("select ROWID, url from mailboxes where url like ?", (acct[0] + "/INBOX/Sent%",))]
            ph = ",".join("?" * len(sent))
            for addr, ts, subj in mdb.execute(f"""select ad.address, m.date_received, s.subject from messages m
                join recipients r on r.message = m.ROWID join addresses ad on ad.ROWID = r.address
                join subjects s on s.ROWID = m.subject
                where m.mailbox in ({ph}) and m.deleted = 0 and m.date_received > ?""", sent + [since]):
                if (subj or "").strip() == REVIEW_EMAIL_SUBJECT: continue
                cid = by_mail.get((addr or "").lower())
                if cid:
                    mails += 1
                    if day(ts) > last.get(cid, ""): last[cid] = day(ts)

    out = {"made": datetime.date.today().isoformat(), "rows": last}
    json.dump(out, open(os.path.join(DATA, "contacted.json"), "w"), separators=(",", ":"))
    print(f"{len(last)} clients texted or emailed in the last {DAYS} days ({texts} texts, {mails} emails), written to jobbook-data/contacted.json")
    print("\nNow open http://127.0.0.1:8942/jobbook.html on this Mac and tap Upload from this Mac.")

if __name__ == "__main__":
    main()

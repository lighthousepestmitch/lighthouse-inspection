#!/usr/bin/env python3
"""Find the Google review asks Mitch has already texted, from Messages on this Mac,
and write jobbook-data/review_asks.json so the Reviews tab shows them as asked.

Only client id and date are written (the data folder is public on GitHub Pages).
Run it any time:  python3 ~/lighthouse-inspection/tools/import_review_asks.py
"""
import sqlite3, re, json, glob, os, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "jobbook-data")
CHAT = os.path.expanduser("~/Library/Messages/chat.db")
MITCH = {"0432181689"}                     # his own mobile, test sends
ASK = re.compile(r"g\.page/r/|google review|review on google", re.I)

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

def main():
    clients = {}
    for f in glob.glob(os.path.join(DATA, "clients_*.json")):
        clients.update(json.load(open(f))["rows"])
    by_phone = {}
    for cid, c in clients.items():
        for ph in re.split(r"[,/;]| or ", c.get("p") or ""):
            n = norm(ph)
            if len(n) >= 9: by_phone.setdefault(n, set()).add(cid)

    db = sqlite3.connect(f"file:{CHAT}?mode=ro", uri=True)
    rows = db.execute("""
      select m.date, m.text, m.attributedBody, m.associated_message_type,
        (select group_concat(h2.id) from chat_message_join cmj
           join chat_handle_join chj on chj.chat_id = cmj.chat_id
           join handle h2 on h2.ROWID = chj.handle_id where cmj.message_id = m.ROWID)
      from message m where m.is_from_me = 1""").fetchall()

    asked, unmatched = {}, {}
    for d, text, ab, assoc, handles in rows:
        if assoc: continue                      # tapbacks ("Loved ...")
        if not ASK.search(body(text, ab)): continue
        day = (datetime.datetime(2001, 1, 1) + datetime.timedelta(seconds=d / 1e9)).date().isoformat()
        for h in (handles or "").split(","):
            n = norm(h)
            if not n or n in MITCH: continue
            ids = by_phone.get(n)
            if not ids: unmatched[n] = max(day, unmatched.get(n, "")); continue
            for cid in ids: asked[cid] = max(day, asked.get(cid, ""))

    out = {"made": datetime.date.today().isoformat(), "rows": asked}
    json.dump(out, open(os.path.join(DATA, "review_asks.json"), "w"), separators=(",", ":"))
    print(f"{len(asked)} clients already asked, written to jobbook-data/review_asks.json")
    if unmatched:
        print(f"{len(unmatched)} numbers asked but not matched to a client:")
        for n, day in sorted(unmatched.items(), key=lambda x: x[1]): print("  ", day, n)

if __name__ == "__main__":
    main()

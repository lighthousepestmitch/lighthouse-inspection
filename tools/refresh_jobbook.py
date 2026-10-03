#!/usr/bin/env python3
"""
Refresh the Job Book's data from Xero.

    python3 tools/refresh_jobbook.py

Reads the newest Xero exports out of ~/Downloads:
  SalesInvoices_*.csv   invoices with line items  (Invoices -> Export, <=500 rows, so filter by date)
  Contacts.csv          phone numbers and emails  (Contacts -> Export)
and merges them into jobbook-data/*.json, keeping everything already there.

Everything fiddly is in here on purpose, so a fresh chat never has to work it out again:
  - pulling the property address out of Mitch's "re: 43 Childe St" line-item notes
  - telling a real address from a job description ("Supply and install Greenzone...")
  - which Xero item code is which service, in everyday words
  - one property + one due date = one visit = one row = one message
  - default intervals per service (Mitch set these 2026-10-04)
  - dropping spreadsheet rows where a job description landed in the name column
"""
import csv, json, os, re, glob, sys, unicodedata, datetime as dt
from collections import defaultdict, Counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "jobbook-data")
DOWN = os.path.expanduser("~/Downloads")
TODAY = dt.date.today()

# ---------------------------------------------------------------- services
NON_SERVICE = {"B", ""}                      # "BANK DETAILS HAVE CHANGED" and bare address lines
SERVICE = {
 "CAS":"General Pest Treatment","CRI":"Cockroach Treatment","CA":"Cockroach & Ant Treatment",
 "ANT":"Ant Treatment","A":"Ant Treatment","ANTEXT":"Ant Treatment","SPE":"Spider Treatment",
 "TI":"Termite Inspection","TI - New Build":"Termite Inspection","TIC/MITE":"Termite Inspection",
 "PPI":"Pre-Purchase Inspection","PI":"Pre-Purchase Inspection",
 "RD":"Rodent Treatment","RDINS":"Rodent Treatment","RDBOX":"Rodent Boxes (supply)",
 "PC":"Pre-Construction Barrier","PCTB":"Post-Construction Barrier",
 "HGB":"Pre-Construction Barrier","HGDPC":"Pre-Construction Barrier","GZ":"Pre-Construction Barrier",
 "NEWRNEW":"Nemesis Renewal","Nemisis New":"Nemesis New",
 "TRELBAIT":"Trelona Baiting","TRELBASE":"Trelona Baiting","TRELMNT":"Trelona Baiting",
 "TRELSTAT":"Trelona Baiting","TERM-NEST":"Termite Treatment",
 "MOS":"Mosquito Treatment","WASP":"Wasp/Bee Treatment","BEE":"Wasp/Bee Treatment",
 "FL":"Flea Treatment","BB":"Bed Bug Treatment","BBI":"Bed Bug Treatment",
 "DF":"Drain Fly Treatment","Fly":"Drain Fly Treatment","CM":"Clothes Moth Treatment",
 "CO":"Carpet Beetle Treatment","LG":"Lawn Grub Treatment","LARV":"Lawn Grub Treatment",
 "TT":"Termite Treatment","Dust":"Termite Treatment","Termite foaming":"Termite Treatment",
 "TDF":"Termite Treatment","AST":"Specialist Treatment","SPOTTREATMENT":"Specialist Treatment",
 "MTHSLV":"Specialist Treatment","OCINSP":"OC Site Assessment","MA":"Specialist Treatment",
 "TMB":"Specialist Treatment","FUN":"Specialist Treatment","BS":"Specialist Treatment",
 "CD":"Specialist Treatment","LA":"Specialist Treatment","SUPG":"Specialist Treatment",
}
# Mitch's sections (set 2026-10-04)
SECTION = {
 "General Pest Treatment":"Pest control","Cockroach Treatment":"Pest control",
 "Cockroach & Ant Treatment":"Pest control","Ant Treatment":"Pest control",
 "Spider Treatment":"Pest control","Rodent Treatment":"Pest control",
 "Termite Inspection":"Termite inspections",
 "Nemesis Renewal":"Baiting systems","Nemesis New":"Baiting systems","Trelona Baiting":"Baiting systems",
 "Pre-Construction Barrier":"Barrier work","Post-Construction Barrier":"Barrier work",
 "Barrier Follow-Up Inspection":"Barrier work",
}
# what comes back around, and how often by default (Mitch, 2026-10-04)
DEFAULT_INTERVAL = {
 "General Pest Treatment":"Biannual","Cockroach Treatment":"Biannual",
 "Cockroach & Ant Treatment":"Biannual","Ant Treatment":"Biannual","Spider Treatment":"Biannual",
 "Rodent Treatment":"Quarterly",
 "Termite Inspection":"Annual",
 "Nemesis Renewal":"Annual","Nemesis New":"Annual","Trelona Baiting":"Annual",
 "Barrier Follow-Up Inspection":"Biannual",
}
MONTHS = {"2 weeks":0.5,"4 weeks":1,"6 weeks":1.5,"8 weeks":2,"Quarterly":3,"Biannual":6,
          "9 months":9,"Annual":12,"2 years":24,"3 years":36,"4 years":48,"5 years":60}
LEGACY = {"3 months":"Quarterly","6 months":"Biannual","12 months":"Annual","1 year":"Annual",
          "one-off":"One-off","1 month":"Quarterly","2 months":"Quarterly"}
# installing a barrier is a one-off job; what repeats is the inspection of it (Mitch, 2026-10-04)
BARRIER_INSTALL = {"Pre-Construction Barrier","Post-Construction Barrier"}
FOLLOWUP = "Barrier Follow-Up Inspection"
RECURRING = set(DEFAULT_INTERVAL)

def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii","ignore").decode()
    return re.sub(r"[^a-z0-9]+","-",s.lower()).strip("-") or "unknown"
def norm_iv(v):
    v = (v or "").strip()
    return LEGACY.get(v.lower(), LEGACY.get(v, v)) or None
def add_months(d, m):
    x = dt.date.fromisoformat(d); return (x + dt.timedelta(days=int(round(m*30.44)))).isoformat()
def nph(p):
    d = re.sub(r"\D","",p or "")
    if d.startswith("61") and len(d) >= 11: d = "0"+d[2:]
    return d if 8 <= len(d) <= 12 else ""
def pdate(s):
    for f in ("%d/%m/%Y","%Y-%m-%d","%d/%m/%y","%d %b %Y"):
        try: return dt.datetime.strptime((s or "").strip(), f).date().isoformat()
        except ValueError: pass
    return None

# ------------------------------------------------- address out of a line item
RE_ADDR = re.compile(r'\b(?:re|ref)\b[:.\s]+(.{3,80})', re.I)
STREETY = re.compile(r'\b(st|street|rd|road|ave|avenue|dr|drive|ln|lane|cres|crescent|ct|court|'
  r'pl|place|way|pde|parade|cl|close|tce|terrace|hwy|highway|blvd|bvd|boulevard|cct|circuit|'
  r'esp|esplanade|grove|pkwy|row|ridge|vista|vsta|view|rise|bay|beach|hill|park)\b', re.I)
WORKWORDS = re.compile(r'\b(supply|install|instyall|treat|treatment|drill|perimeter|slab|chemical|'
  r'collar|penetration|foam|reo|rep-band|protectacote|homeguard|greenzone|per\s*l/m|'
  r'sections?|edge|cuts?|joint|houses?\s*@|ea\b|\$)', re.I)
SUBURBS = r"(byron bay|ballina|lennox head|bangalow|brunswick heads|mullumbimby|ocean shores|" \
          r"alstonville|suffolk park|ewingsdale|possum creek|tumbulgam|skinners shoot|nsw|australia)"

def addr_from(desc):
    """Mitch writes 're: 43 Childe St'. He also writes 're. Supply and install...'. Only take addresses."""
    m = RE_ADDR.search(desc or "")
    if not m: return None
    a = re.split(r'[\n]|\s+\(', m.group(1).strip(" .-\n"))[0].strip()
    a = re.split(r'\.\s|\.$', a)[0].strip(" .,-")
    a = re.sub(r'\s{2,}.*$','',a).strip()
    if not (4 <= len(a) <= 60) or not re.search(r'[A-Za-z]', a): return None
    numlead = bool(re.match(r'^(unit\s+|apt\s+|shop\s+|lot\s+)?\d+[a-z]?\s*(/\s*\d+[a-z]?)?\s+\S', a, re.I))
    if WORKWORDS.search(a) and not numlead: return None
    if numlead or STREETY.search(a) or re.search(SUBURBS, a, re.I): return a
    return None

def pkey(a):
    """Street number + first 5 letters of the street name. Merges '58 Ruskin' with '58 Ruskin St, Byron Bay'."""
    s = (a or "").lower()
    s = re.sub(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}.*$"," ",s)
    s = re.sub(r"\(.*?\)"," ",s); s = re.sub(SUBURBS," ",s)
    s = re.sub(r"\b(street|st|road|rd|drive|dr|avenue|ave|lane|ln|crescent|cres|court|ct|place|pl|"
               r"way|parade|pde|close|cl|terrace|tce|highway|hwy)\b"," ",s)
    s = re.sub(r"[^a-z0-9/ ]"," ",s); s = re.sub(r"\s+"," ",s).strip()
    m = re.match(r"^([0-9]+[a-z]?(?:/[0-9]+[a-z]?)?)\s+(.*)$", s)
    if m:
        w = re.sub(r"[^a-z]","",(m.group(2) or "").split(" ")[0])
        k = m.group(1).replace(" ","") + (w[:5] if w else "")
    else:
        k = "".join(x[:5] for x in [x for x in s.split(" ") if x][:2]) or "main"
    return re.sub(r"[^A-Za-z0-9_\-.~:@+]","-",k)

def newest(pattern):
    hits = glob.glob(os.path.join(DOWN, pattern))
    return max(hits, key=os.path.getmtime) if hits else None

def load(name):
    p = os.path.join(DATA, name + ".json")
    return json.load(open(p)) if os.path.exists(p) else None

def main():
    inv_csv = newest("SalesInvoices_*.csv")
    con_csv = newest("Contacts*.csv")
    if not inv_csv:
        sys.exit("No SalesInvoices_*.csv in ~/Downloads. In Xero: Invoices -> Search (set a date range\n"
                 "so it's under 500) -> Export.")
    print(f"invoices : {os.path.basename(inv_csv)}")
    print(f"contacts : {os.path.basename(con_csv) if con_csv else '(none - phones/emails not refreshed)'}")

    settings = load("settings") or {}
    manifest = settings.get("manifest", {})

    clients, props, jobs, due = {}, {}, defaultdict(list), []
    for i in range(manifest.get("clients",0)):  clients.update((load(f"clients_{i}")  or {}).get("rows",{}))
    for i in range(manifest.get("properties",0)): props.update((load(f"properties_{i}") or {}).get("rows",{}))
    for i in range(manifest.get("jobs",0)):
        for pid, js in (load(f"jobs_{i}") or {}).get("rows",{}).items(): jobs[pid] += js
    for i in range(manifest.get("due",0)):      due += (load(f"due_{i}") or {}).get("rows",[])
    replies = {}
    for i in range(manifest.get("replies",0)):  replies.update((load(f"replies_{i}") or {}).get("rows",{}))
    before = (len(clients), len(props), sum(len(v) for v in jobs.values()))
    seen_inv = {j["i"] for v in jobs.values() for j in v if j.get("i")}

    # ---- group the export's line rows into invoices
    rows = list(csv.DictReader(open(inv_csv, encoding="utf-8-sig")))
    bundle = defaultdict(lambda: {"lines":[]})
    for r in rows:
        n = (r.get("InvoiceNumber") or "").strip()
        if not n: continue
        b = bundle[n]
        b.setdefault("date", pdate(r.get("InvoiceDate")))
        b.setdefault("contact", (r.get("ContactName") or "").strip())
        b.setdefault("status", (r.get("Status") or "").strip())
        b.setdefault("total", r.get("Total"))
        b.setdefault("sa", [(r.get("SAAddressLine1") or "").strip(), (r.get("SACity") or "").strip(),
                            (r.get("SARegion") or "").strip(), (r.get("SAPostalCode") or "").strip()])
        b["lines"].append({"code": (r.get("InventoryItemCode") or "").strip(),
                           "desc": (r.get("Description") or "").strip(),
                           "amt":  (r.get("LineAmount") or "").strip()})

    added_c = added_p = added_j = 0
    newdue = []
    for n, v in sorted(bundle.items(), key=lambda kv: kv[1].get("date") or ""):
        if not v.get("date") or n in seen_inv: continue
        name = v["contact"]
        if not name: continue
        cid = slug(name)
        if cid not in clients:
            src = [x for x in v["sa"] if x]
            clients[cid] = {"n":name,"e":"","p":"","g":"active",
                            "a":[src[0] if src else "", src[1] if len(src)>1 else "",
                                 src[2] if len(src)>2 else "", src[3] if len(src)>3 else ""],
                            "iv":"","ic":0}
            added_c += 1
        clients[cid]["ic"] = clients[cid].get("ic",0) + 1

        svcs, marker = [], None
        for l in v["lines"]:
            a = addr_from(l["desc"])
            if a and not marker: marker = a
            if l["code"] in NON_SERVICE: continue
            svcs.append({"s": SERVICE.get(l["code"], "Other"), "d": l["desc"][:110]})
        if not svcs: continue

        base = clients[cid]
        label = marker or (base["a"][0] if base["a"][0] else "")
        pid = cid + "::" + (pkey(label) if label else "main")
        if pid not in props:
            props[pid] = {"c":cid, "l":label or "(address unknown)", "n":0}
            added_p += 1
        props[pid]["n"] = props[pid].get("n",0) + 1
        jobs[pid].insert(0, {"i":n, "d":v["date"], "t":v["total"], "s":svcs})
        added_j += 1

        for s in {x["s"] for x in svcs}:
            if s in BARRIER_INSTALL:
                # the install is one-off; book its inspection instead (Mitch, 2026-10-04)
                newdue.append({"p":pid, "s":FOLLOWUP, "ld":v["date"],
                               "iv":DEFAULT_INTERVAL[FOLLOWUP],
                               "nd":add_months(v["date"], MONTHS[DEFAULT_INTERVAL[FOLLOWUP]]),
                               "src":"after a barrier install"})
            elif s in RECURRING:
                iv = norm_iv(clients[cid].get("iv")) or DEFAULT_INTERVAL[s]
                newdue.append({"p":pid, "s":s, "ld":v["date"], "iv":iv,
                               "nd":add_months(v["date"], MONTHS[iv]) if MONTHS.get(iv) else None,
                               "src":"default for this service"})

    # Installing a barrier is a one-off. What repeats is the inspection of it.
    # Any barrier-install row (old seed data included) becomes a follow-up inspection.
    converted = 0
    rewritten = []
    for d in due:
        if d["s"] in BARRIER_INSTALL:
            converted += 1
            rewritten.append({"p":d["p"], "s":FOLLOWUP, "ld":d.get("ld"),
                              "iv":DEFAULT_INTERVAL[FOLLOWUP],
                              "nd":add_months(d["ld"], MONTHS[DEFAULT_INTERVAL[FOLLOWUP]]) if d.get("ld") else None,
                              "src":"after a barrier install"})
        else:
            rewritten.append(d)
    due = rewritten

    # Anything still without a repeat gets the default for its service (Mitch's six answers).
    # Flagged as a default so the app can show it as one he can change.
    defaulted = 0
    for d in due:
        if not d.get("nd") and d.get("ld") and d["s"] in DEFAULT_INTERVAL:
            iv = DEFAULT_INTERVAL[d["s"]]
            d["iv"], d["nd"], d["src"] = iv, add_months(d["ld"], MONTHS[iv]), "default for this service"
            defaulted += 1

    # newest job per property+service wins
    merged = {}
    for d in due + newdue:
        k = (d["p"], d["s"])
        if k not in merged or (d.get("ld") or "") > (merged[k].get("ld") or ""): merged[k] = d
    due = list(merged.values())

    # ---- fill in phone / email / address from the contacts export
    filled = 0
    if con_csv:
        by_slug, by_em = {}, {}
        for r in csv.DictReader(open(con_csv, encoding="utf-8-sig")):
            nm = (r.get("*ContactName") or r.get("ContactName") or "").strip()
            rec = {"p": nph(r.get("PhoneNumber") or r.get("MobileNumber") or ""),
                   "e": (r.get("EmailAddress") or "").strip(),
                   "a": [(r.get("SAAddressLine1") or r.get("POAddressLine1") or "").strip(),
                         (r.get("SACity") or r.get("POCity") or "").strip().title(),
                         (r.get("SARegion") or r.get("PORegion") or "").strip(),
                         (r.get("SAPostalCode") or r.get("POPostalCode") or "").strip()]}
            if nm: by_slug[slug(nm)] = rec
            if rec["e"]: by_em.setdefault(rec["e"].lower(), rec)
        for cid, c in clients.items():
            r = by_slug.get(cid) or (by_em.get(c["e"].lower()) if c.get("e") else None)
            if not r: continue
            if r["p"] and not c.get("p"): c["p"] = r["p"]; filled += 1
            if r["e"] and not c.get("e"): c["e"] = r["e"]
            if r["a"][0] and not (c.get("a") or [""])[0]: c["a"] = r["a"]

    # ---- rewrite the data files
    def chunk(obj, n, name, islist=False):
        items = list(enumerate(obj)) if islist else list(obj.items())
        cnt = 0
        for i in range(0, max(len(items),1), n):
            part = items[i:i+n]
            body = {"rows": [v for _, v in part]} if islist else {"rows": dict(part)}
            json.dump(body, open(os.path.join(DATA, f"{name}_{cnt}.json"), "w"), separators=(",",":"))
            cnt += 1
        for stale in glob.glob(os.path.join(DATA, f"{name}_*.json")):
            if int(re.search(r"_(\d+)\.json$", stale).group(1)) >= cnt: os.remove(stale)
        return cnt

    man = {"clients": chunk(clients,300,"clients"),
           "properties": chunk(props,400,"properties"),
           "jobs": chunk({k:sorted(v,key=lambda j:j.get("d") or "",reverse=True) for k,v in jobs.items()},220,"jobs"),
           "due": chunk(due,400,"due",islist=True),
           "replies": chunk(replies,140,"replies") if replies else 0}
    last = max((j["d"] for v in jobs.values() for j in v if j.get("d")), default=None)
    settings["manifest"] = man
    settings["seedDataEnds"] = last
    settings["refreshedOn"] = TODAY.isoformat()
    settings["sections"] = SECTION
    settings["defaultIntervals"] = DEFAULT_INTERVAL
    json.dump(settings, open(os.path.join(DATA,"settings.json"),"w"), separators=(",",":"))
    json.dump({"lastSyncedDate": last, "note": "refreshed from a Xero export"},
              open(os.path.join(DATA,"sync.json"),"w"))

    live = [d for d in due if d.get("nd")]
    od = [d for d in live if d["nd"] < TODAY.isoformat()]
    age = lambda d: (TODAY - dt.date.fromisoformat(d["nd"])).days
    print(f"\n  new clients    {added_c:5}   (now {len(clients)}, was {before[0]})")
    print(f"  new properties {added_p:5}   (now {len(props)}, was {before[1]})")
    print(f"  new jobs       {added_j:5}   (now {before[2]+added_j}, was {before[2]})")
    print(f"  phones filled  {filled:5}")
    print(f"  barrier installs turned into follow-up inspections: {converted}")
    print(f"  blanks filled with your service defaults:            {defaulted}")
    print(f"  last job now   {last}")
    print(f"\n  ring today (<=3mo over) {sum(1 for d in od if age(d)<=92):5}")
    print(f"  well overdue            {sum(1 for d in od if 92<age(d)<=730):5}")
    print(f"  old barriers (2yr+)     {sum(1 for d in od if age(d)>730):5}")
    print(f"  needs an interval       {sum(1 for d in due if not d.get('nd')):5}")
    print(f"\n  files written to {DATA}")
    print("  commit, then Mitch runs:  git -C ~/lighthouse-inspection push")

if __name__ == "__main__":
    main()
    import build_service_lists; build_service_lists.main()

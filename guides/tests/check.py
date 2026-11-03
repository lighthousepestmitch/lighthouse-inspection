#!/usr/bin/env python3
"""Checks for the Field Guides page. Python 3 standard library only, plus macOS's built-in JavaScriptCore shell (or Node) for the JS.
Run from anywhere:  python3 guides/tests/check.py
No browser, no server. It does not touch the network or any file outside guides/ (it only reads Capture's index.html for the colour tokens)."""
import json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
G = os.path.dirname(HERE)                        # guides/
REPO = os.path.dirname(G)
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
EMDASH = chr(0x2014)

fails = 0
def ok(cond, msg):
    global fails
    if not cond:
        fails += 1
    print(("ok    " if cond else "FAIL  ") + msg)

def read(*p):
    with open(os.path.join(G, *p), encoding="utf-8") as f:
        return f.read()

def js_runner():
    node = shutil.which("node")
    if node:
        return ("node", node)
    if os.path.exists(JSC):
        return ("jsc", JSC)
    return (None, None)

# ---------------------------------------------------------------- JSON
files = sorted(os.listdir(os.path.join(G, "data")))
data = {}
for fn in files:
    try:
        data[fn] = json.loads(read("data", fn))
        ok(True, "data/%s parses" % fn)
    except Exception as e:
        ok(False, "data/%s does not parse: %s" % (fn, e))
try:
    json.loads(read("manifest.webmanifest")); ok(True, "manifest.webmanifest parses")
except Exception as e:
    ok(False, "manifest.webmanifest: %s" % e)

idx = data.get("index.json", {})
entries = idx.get("guides", [])
ids = [e["id"] for e in entries]
ok(len(ids) == len(set(ids)), "index.json ids are unique")
ok(len(set(e["order"] for e in entries)) == len(entries), "index.json order numbers are unique")
for e in entries:
    ok(e["id"] + ".json" in data, "guide file exists for index entry %s" % e["id"])
    g = data.get(e["id"] + ".json", {})
    ok(g.get("id") == e["id"], "%s: id inside file matches" % e["id"])
    ok(g.get("updated") == e.get("updated"), "%s: updated date matches index.json" % e["id"])
    ok(g.get("title") == e.get("title"), "%s: title matches index.json" % e["id"])
extra = [fn for fn in files if fn != "index.json" and fn[:-5] not in ids]
ok(not extra, "no guide file missing from index.json %s" % extra)

# ---------------------------------------------------------------- text rules (every file in guides/)
alltext = {}
for root, _, fns in os.walk(G):
    for fn in fns:
        p = os.path.join(root, fn)
        rel = os.path.relpath(p, G)
        if fn.endswith((".json", ".js", ".css", ".html", ".md", ".py", ".webmanifest")):
            with open(p, encoding="utf-8") as f:
                alltext[rel] = f.read()
ok(not [r for r, t in alltext.items() if EMDASH in t], "no em dash (U+2014) anywhere in guides/ %s" % [r for r, t in alltext.items() if EMDASH in t])
datatext = {r: t for r, t in alltext.items() if r.startswith("data/")}
for bad in ["5085" + "573", "5805" + "573"]:   # built in pieces so this file does not trip its own scan
    ok(not [r for r, t in alltext.items() if bad in t], "licence number %s not printed anywhere in guides/" % bad)
ok(not [r for r, t in alltext.items() if re.search(r"\bLic(ence|\.)", t) and r.startswith("data/")], "no licence wording in guide data")
ok(not [r for r, t in datatext.items() if "$" in t], "no dollar amounts in guide data")
ok(not [r for r, t in datatext.items() if re.search(r"\b(price|prices|pricing|discount|margin|ex GST)\b", t, re.I) and r != "data/every-job.json"],
   "no price words in guide data (every-job only has the never-discuss-pricing line)")
pj = datatext["data/every-job.json"]
ok(len(re.findall(r"pric", pj, re.I)) == 1, "every-job has exactly one pricing mention (the never discuss pricing rule)")
phones = set()
for r, t in datatext.items():
    for m in re.findall(r"(?<!\d)(?:\+?61|0)[\d ]{8,13}\d(?!\d)", t):
        phones.add(re.sub(r"\s", "", m))
ok(phones <= {"0432181689"}, "only the business phone appears in guide data %s" % sorted(phones))
for bad in ["Google Calendar", "Google Drive", "Casual", "Andrew", "Wayne", "Marniya"]:
    ok(not [r for r, t in datatext.items() if bad in t], "guide data has no '%s'" % bad)
ok(len(re.findall("warranty", pj)) == 1, "every-job uses 'warranty' once (only in 'free service period, not a warranty')")
mira = {r: len(re.findall("Mira", t)) for r, t in datatext.items() if "Mira" in t}
ok(mira == {"data/termite-inspection.json": 1}, "Mira appears once, in the termite file only %s" % mira)
term = datatext["data/termite-inspection.json"]
ok("Mitch builds the Mira report from it" in term, "the one Mira mention is the office-builds-the-report sentence")
ok("Job Calendar" in datatext["data/every-job.json"], "every-job says Job Calendar")

# legal wording kept as written (only Mira swapped for Capture)
legal = [
    "75mm minimum visual inspection zone must be maintained below weepholes and slab edges at all times. Record EVERY location where this is not met and the specific reason.",
    "AS 3660.2: 75mm minimum inspection zone below weepholes and slab edges. Document every location where not achieved and the specific reason why.",
    "\\\"No access\\\" alone is not sufficient. State exactly what prevented entry.",
    "\\\"Unable to access all areas in accordance with AS 4349.3\\\" must be noted in Capture.",
    "Every inaccessible, obstructed or not-inspected area MUST be documented in Capture with the specific reason. \\\"No access\\\" alone is not sufficient. State what was present, why access was not possible, and what clearance was available if measurable.",
    "Partial access must be documented. State which areas were reached and which were not, and the specific reason.",
]
for s in legal:
    ok(s in term, "legal wording kept: " + s[:60].replace("\\", "") + "...")
for fn in ["cas", "cockroach-only", "ant-only", "spider-only"]:
    t = datatext["data/%s.json" % fn]
    d = data["%s.json" % fn]
    qa = [i for s in d["sections"] if s["type"] == "qa" for i in s["items"]]
    safe = [i for i in qa if "supermarket fly spray" in i["a"]]
    ok(len(safe) == 1 and "[Mitch to confirm this wording]" in safe[0]["a"], "%s: supermarket fly spray answer kept and marked" % fn)
ok("less than your average supermarket fly spray" in datatext["data/cas.json"], "CAS safety answer kept exactly as written")

# ---------------------------------------------------------------- JS: syntax, then render every guide
kind, runner = js_runner()
ok(runner is not None, "a JavaScript engine is available (%s)" % kind)
if runner:
    for fn in ["app.js", "render.js", "sw.js", "tests/render-all.js"]:
        if kind == "node":
            r = subprocess.run([runner, "--check", os.path.join(G, fn)], capture_output=True, text=True)
        else:
            r = subprocess.run([runner, "-e", "try{checkSyntax(%s);print('SYNTAX_OK')}catch(e){print('SYNTAX_ERR '+e)}" % json.dumps(os.path.join(G, fn))], capture_output=True, text=True)
        good = r.returncode == 0 and (kind == "node" or "SYNTAX_OK" in r.stdout)
        ok(good, "syntax ok: %s %s" % (fn, "" if good else (r.stdout + r.stderr).strip()[:200]))
    cmd = [runner, "tests/render-all.js"] if kind == "node" else [runner, "tests/render-all.js"]
    r = subprocess.run(cmd, cwd=G, capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith("{")]
    rep = json.loads(line[-1]) if line else None
    ok(rep is not None, "render harness ran %s" % ("" if rep else (r.stdout + r.stderr)[:300]))
    if rep:
        for g in rep["guides"]:
            n = g["id"]
            ok(not g["problems"], "%s: schema ok %s" % (n, g["problems"]))
            ok(g["blocksRendered"] == g["sections"], "%s: all %d sections rendered (%d blocks in the html)" % (n, g["sections"], g["blocksRendered"]))
            ok(not g["missingHeadings"], "%s: every heading is on the page %s" % (n, g["missingHeadings"]))
            ok(g["markers"] == g["chips"], "%s: every [Mitch to confirm] marker shows as a visible marker (%d)" % (n, g["markers"]))
            ok(g["tickTotal"] == g["checklistItems"], "%s: %d tick boxes for %d checklist items" % (n, g["tickTotal"], g["checklistItems"]))
            ok(g["tickedAfter"] == (1 if g["checklistItems"] else 0) and g["tickInputsChecked"] == g["tickedAfter"], "%s: a saved tick comes back ticked" % n)
        ok(all(rep["cards"].values()), "home list has a card for every guide")
        ok("&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;" in rep["escape"] and 'class="confirm"' in rep["escape"], "text from JSON is escaped, markers still show")
        ok(rep["fmt"] == "4 Nov 2026", "dates read like 4 Nov 2026")
        sh = rep["searchHits"]
        ok("termite-inspection" in sh["subfloor"] and "cas" in sh["subfloor"], "search finds subfloor in the right guides %s" % sh["subfloor"])
        ok(set(sh["ppe"]) >= {"every-job", "vehicle-check"}, "search finds PPE %s" % sh["ppe"])
        ok(sh["zzzz"] == [], "search with no match returns nothing")
        print("\nSections per guide:")
        for g in rep["guides"]:
            print("  %-20s %2d sections, %3d tick boxes, %d markers" % (g["id"], g["sections"], g["tickTotal"], g["markers"]))

# ---------------------------------------------------------------- service worker (static: no browser, so the assets are checked by reading it)
sw = read("sw.js"); app = read("app.js"); page = read("index.html")
core = re.search(r"const CORE = (\[.*?\]);", sw, re.S); logos = re.search(r"const LOGOS = (\[.*?\]);", sw, re.S)
ok(bool(core and logos), "sw.js lists CORE and LOGOS")
CORE = json.loads(core.group(1)); LOGOS = json.loads(logos.group(1))
def exists(rel):
    rel = "index.html" if rel in ("./", "") else rel
    return os.path.exists(os.path.normpath(os.path.join(G, rel)))
for a in CORE + LOGOS:
    ok(exists(a), "sw.js asset exists: %s" % a)
refs = set(re.findall(r'(?:href|src)="([^"#]+)"', page))
local = {r for r in refs if not r.startswith(("http:", "https:", "data:"))}
listed = set(CORE) | set(LOGOS)
missing = sorted(r for r in local if r not in listed)
ok(not missing, "every local asset index.html uses is listed in sw.js %s" % missing)
ok(not [r for r in local if not exists(r)], "every local asset index.html uses exists")
mani = json.loads(read("manifest.webmanifest"))
ok(all(exists(i["src"]) for i in mani["icons"]), "manifest icons exist")
ok(all(exists(i["src"]) or i["src"] in LOGOS for i in mani["icons"]) and all(i["src"] in LOGOS for i in mani["icons"]), "manifest icons are in the sw.js logo list")
ok(all(a in CORE for a in ["data/index.json", "app.js", "render.js", "guides.css", "lpc-look.css", "manifest.webmanifest", "index.html"]) or "index.html" in CORE, "sw.js core has page, scripts, styles, manifest and index")
ok("install" in sw and "activate" in sw and "fetch" in sw and "skipWaiting" in sw and "clients.claim" in sw, "sw.js has install, activate, fetch, skipWaiting, claim")
ok("data/\" + g.id + \".json" in sw, "sw.js saves every guide listed in data/index.json at install")
b = re.search(r'const BUILD = "([^"]+)"', app).group(1); v = re.search(r'const VERSION = "([^"]+)"', sw).group(1)
ok(b == v, "BUILD in app.js (%s) matches VERSION in sw.js (%s)" % (b, v))
ok('set("x-guides-cache", "1")' in sw and 'get("x-guides-cache") === "1"' in app, "offline tag matches between sw.js and app.js")
ok("controllerchange" in app and 'id="newver"' in page and "Tap to refresh" in page, "new version banner is wired up")
ok('id="build"' in page and "build" in app, "build string is shown at the bottom")

# ---------------------------------------------------------------- look
css = read("guides.css") + read("lpc-look.css")
ok("prefers-color-scheme" not in css and "data-theme" not in css, "always light: no dark mode in the css")
ok("@page" in css and "@media print" in css and "size:A4" in css, "print stylesheet for A4 is there")
cap_path = os.path.join(REPO, "index.html")
if os.path.exists(cap_path):
    with open(cap_path, encoding="utf-8") as f:
        cap = f.read()
    m = re.search(r":root\{(.*?)\n  \}", cap, re.S)
    capvars = dict(re.findall(r"(--[\w-]+):([^;]+);", m.group(1))) if m else {}
    mine = dict(re.findall(r"(--[\w-]+):([^;]+);", re.search(r":root\{(.*?)\}", read("lpc-look.css"), re.S).group(1)))
    diff = {k: (mine[k], capvars.get(k)) for k in mine if capvars.get(k, "").strip() != mine[k].strip()}
    ok(bool(capvars) and not diff, "lpc-look.css tokens match Capture's :root exactly %s" % diff)
else:
    print("skip  Capture's index.html not found, token comparison skipped")
ok("Public+Sans" in page and "Newsreader" in page and "Spline+Sans+Mono" in page, "page loads the same Google Fonts as Capture")

print("\n%s" % ("ALL CHECKS PASSED" if not fails else "%d CHECK(S) FAILED" % fails))
sys.exit(1 if fails else 0)

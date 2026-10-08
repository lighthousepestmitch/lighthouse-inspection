"""Builds jobbook-data/services.json for records.html: each Xero service type with its
invoice description wordings, most used first.

services.json is PUBLIC (served with the website), so no customer detail may reach it. Mitch writes
the property into the line item ("re: 43 Childe St", "@ 39 Brandon st", "re: The Northcott Society, 14 Martin St")
and sometimes a proposal number or a vehicle rego. This script cuts all of that off, and anything that
still looks like an address afterwards is dropped whole. Run on its own, or it runs at the end of
refresh_jobbook.py. It prints how many wordings it cut or dropped, and refuses to write the file if a
street address is still in it."""
import json, os, re, glob, collections, sys

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jobbook-data")

# words that mean "this is the job", so 're. Supply and install HomeGuard collars' is kept
KEEP_WORDS = (r"supply|supplied|install|installation|treat|treatment|chemical|drill|drilled|partial|internal|external|"
              r"perimeter|slab|spot|spray|foam|dust|bait|baiting|monitoring|inspection|inspect|renewal|first|"
              r"homeguard|greenzone|termidor|fipronil|bifenthrin|nemesis|trelona|occupation|certificate|certification")
STREET = (r"st|street|rd|road|ave|avenue|dr|drive|ln|lane|cres|crescent|ct|court|pl|place|way|pde|parade|"
          r"cl|close|tce|terrace|hwy|highway|blvd|boulevard|cct|circuit|esp|esplanade|grove|pkwy|row|ridge|"
          r"view|rise|bay|beach|hill|park|trail|track|walk|lookout")
SUBURB = (r"byron bay|ballina|lennox head|bangalow|brunswick heads|brunswick|mullumbimby|ocean shores|alstonville|"
          r"suffolk park|suffo|ewingsdale|possum creek|tumbulgam|skinners shoot|cumbalum|wollongbar|lismore|"
          r"goonellabah|murwillumbah|tweed|kingscliff|casino|evans head|wategos|broken head|newrybar|federal|"
          r"nimbin|clunes|mullum|east ballina|west ballina|nsw|qld")

# a marker that points at something job-specific: "re", "re:", "re.", "re :", "@", "at", "for"
MARK = re.compile(r"(?:(?<![A-Za-z])re(?![A-Za-z-])\.?\s*:?|(?<![A-Za-z])ref(?![A-Za-z])\.?\s*:?|@|\bat\b|\bfor\b)", re.I)
# proposal / agreement / renewal numbers carry the street number plus letters of the client or street
CODE = re.compile(r"\b(?:(?:as\s+)?per\s+|as\s+|re\.?:?\s*)?(?:proposal|agreement|quote|renewal|invoice|job|order|contract)\s*(?:no\.?|number|#)\b.*$", re.I)
REGO = re.compile(r"(?:\s+to)?\s+(?:a\s+)?(?:vehicle|car|ute|van|truck)\s+(?:registration|rego|reg)\b.*$", re.I)
# anything that still looks like an address after the cuts: the description is dropped whole
NUMSTREET = re.compile(r"\b\d+[a-z]?(?:\s*[/-]\s*\d+[a-z]?)?\s+(?:[A-Za-z'.-]+\s+){0,3}?(?:" + STREET + r")\b", re.I)
POSTCODE = re.compile(r"\b(?:24[6-9]\d|25\d\d|42\d\d)\b(?!\.\d)")
SUBURB_RE = re.compile(r"\b(?:" + SUBURB + r")\b", re.I)
KEEP_RE = re.compile(r"^(?:" + KEEP_WORDS + r")\b", re.I)

def cut(d):
    d = REGO.sub("", d)
    d = CODE.sub("", d)
    # cut from the first marker whose tail is an address, a name, or has a number in it. A tail that is plain
    # lowercase work ("at kitchen area", "re. chemical treatment to slab cuts") or starts with a work or
    # product word ("re. Supply and install HomeGuard collars") is kept, if it has no address in it.
    for m in MARK.finditer(d):
        tail = d[m.end():].lstrip(" .:-,")
        if (not tail or tail[0].isdigit() or re.match(r"(?:unit|apt|shop|lot|level|suite)\b", tail, re.I)
                or NUMSTREET.search(tail) or SUBURB_RE.search(tail) or POSTCODE.search(tail)
                or (tail[0].isupper() and not KEEP_RE.match(tail))):
            d = d[:m.start()]
            break
    return re.sub(r"\s+", " ", d).strip(" .,-–:@")

def addressy(d):
    return bool(NUMSTREET.search(d) or POSTCODE.search(d) or SUBURB_RE.search(d))

def main():
    svc, desc = collections.Counter(), collections.defaultdict(collections.Counter)
    cutn = dropped = 0
    for f in glob.glob(os.path.join(DATA, "jobs_*.json")):
        for jobs in json.load(open(f)).get("rows", {}).values():
            for j in jobs:
                for s in j.get("s", []):
                    name = (s.get("s") or "").strip()
                    if not name: continue
                    svc[name] += 1
                    raw = (s.get("d") or "").strip()
                    d = cut(raw)
                    if d != raw.strip(" .-–"): cutn += 1
                    if addressy(d): dropped += 1; continue
                    if 3 <= len(d) <= 140 and not re.search(r"\d{2}/\d{1,2}/\d{2}", d):
                        desc[name][d] += 1
    out = {"services": [{"s": n, "n": c, "d": [d for d, _ in desc[n].most_common(12)]}
                        for n, c in svc.most_common()]}
    bad = [d for s in out["services"] for d in s["d"] if addressy(d)]
    if bad:
        sys.exit("services.json NOT written: a street address is still in these wordings: " + "; ".join(bad[:3]))
    json.dump(out, open(os.path.join(DATA, "services.json"), "w"), separators=(",", ":"))
    print(f"  services.json: {len(out['services'])} service types "
          f"({cutn} wordings had a job-specific tail cut off, {dropped} dropped whole because they still looked like an address)")

if __name__ == "__main__":
    main()

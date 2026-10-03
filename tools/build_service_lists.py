"""Builds jobbook-data/services.json for records.html: each Xero service type with its
invoice description wordings, most used first. Address tails ("re 3 Hayter St") are cut off.
Run on its own, or it runs at the end of refresh_jobbook.py."""
import json, os, re, glob, collections

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jobbook-data")
TAIL = re.compile(r"\s+(re\.?:?|at|for|as per)\s+\d.*$|\s+re\.?:?\s*$", re.I)

def main():
    svc, desc = collections.Counter(), collections.defaultdict(collections.Counter)
    for f in glob.glob(os.path.join(DATA, "jobs_*.json")):
        for jobs in json.load(open(f)).get("rows", {}).values():
            for j in jobs:
                for s in j.get("s", []):
                    name = (s.get("s") or "").strip()
                    if not name: continue
                    svc[name] += 1
                    d = TAIL.sub("", (s.get("d") or "").strip()).strip(" .-–")
                    if 3 <= len(d) <= 140 and not re.search(r"\d{2}/\d{1,2}/\d{2}", d):
                        desc[name][d] += 1
    out = {"services": [{"s": n, "n": c, "d": [d for d, _ in desc[n].most_common(12)]}
                        for n, c in svc.most_common()]}
    json.dump(out, open(os.path.join(DATA, "services.json"), "w"), separators=(",", ":"))
    print(f"  services.json: {len(out['services'])} service types")

if __name__ == "__main__":
    main()

"""Quality audit: flag retrieved .txt files that look like hits but aren't real
full text (bot-walls, abstract-only, references-only, truncated).

Usage:  python audit_quality.py [out_dir ...]   (default: out demo_results_0)
"""
import csv, glob, os, re, sys

BOTWALL = re.compile(
    r"not a bot|Anubis|Just a moment|enable JavaScript|Access Denied|"
    r"Cloudflare|Checking your browser|Proof-of-Work|verify you are human",
    re.I)

# a line that is really a bibliography entry / citation-list furniture
REF_LINE = re.compile(
    r"Google Scholar|CrossRef|PubMed|\[DOI\]|\[PMC|PMC free article|"
    r"doi\.org/10\.|Crossref Citations|Cited by", re.I)

# section headings a real paper body tends to contain
SECTION = re.compile(
    r"^\s*(abstract|introduction|background|method|materials|results|"
    r"discussion|conclusion|references)\b", re.I | re.M)

def analyse(path, source):
    raw = open(path, encoding="utf-8", errors="replace").read()
    n = len(raw)
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    nlines = max(1, len(lines))

    # where do the references start? (first 'References' heading, else first
    # run of reference-looking lines)
    ref_start = None
    m = re.search(r"^\s*references\s*$", raw, re.I | re.M)
    if m:
        ref_start = m.start()
    body = raw[:ref_start] if ref_start is not None else raw
    body_chars = len(body.strip())

    ref_lines = sum(1 for ln in lines if REF_LINE.search(ln))
    ref_frac = ref_lines / nlines
    sections = set(x.lower() for x in SECTION.findall(raw))
    # body sections = anything other than abstract/references
    real_body_sections = sections - {"abstract", "references"}
    has_cited_by = bool(re.search(r"cited by|crossref citations", raw, re.I))

    flags = []
    if BOTWALL.search(raw[:3000]):
        flags.append("BOTWALL")            # saved a challenge page, not a paper
    # abstract/refs only: little prose before references, no real body sections
    if body_chars < 3500 and not real_body_sections and n < 25000:
        flags.append("NO_BODY")            # abstract + references only
    elif ref_frac > 0.5 and not real_body_sections:
        flags.append("MOSTLY_REFS")        # bibliography dominates, body missing
    if n < 1500 and not flags:
        flags.append("VERY_SHORT")

    return dict(chars=n, body_chars=body_chars, ref_frac=round(ref_frac, 2),
                body_sections=",".join(sorted(real_body_sections)) or "-",
                cited_by=has_cited_by, source=source, flags=flags)


def main():
    dirs = sys.argv[1:] or ["out", "demo_results_0"]
    base = os.path.dirname(os.path.abspath(__file__))
    for d in dirs:
        dpath = os.path.join(base, d)
        if not os.path.isdir(dpath):
            continue
        man = os.path.join(dpath, "_manifest.csv")
        src_by_id = {}
        if os.path.exists(man):
            for r in csv.DictReader(open(man, encoding="utf-8")):
                src_by_id[r["pub_id"]] = r.get("source", "")
        files = sorted(glob.glob(os.path.join(dpath, "pub.*.txt")))
        flagged = []
        for f in files:
            pid = os.path.basename(f)[:-4]
            a = analyse(f, src_by_id.get(pid, "?"))
            if a["flags"]:
                flagged.append((pid, a))
        print("=" * 78)
        print(f"{d}/   {len(files)} files scanned   ->   {len(flagged)} FLAGGED")
        print("=" * 78)
        # group by flag
        for pid, a in sorted(flagged, key=lambda x: x[1]["flags"][0]):
            fl = " ".join(a["flags"])
            print(f"  {pid}  [{a['source']:13}] {a['chars']:>7}c "
                  f"body={a['body_chars']:>6}c refs={a['ref_frac']:.0%} "
                  f"sects={a['body_sections'][:30]:30} -> {fl}")
        if not flagged:
            print("  (nothing flagged — all files look like real full text)")
        print()


if __name__ == "__main__":
    main()

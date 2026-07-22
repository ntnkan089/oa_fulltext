"""Export only the CLEAN full-text papers from a run, ready for a downstream
embedding / RAG pipeline. Filters out the garbage the validator flags
(non_article / stub / refs_only / bot-wall) so you never embed a correction
notice or an abstract-stub.

Two outputs in the run folder:
  clean_corpus.jsonl   one JSON line per clean paper:
                       {"pub_id","doi","publisher","source","chars","text"}
  clean_index.csv      the manifest rows that made the cut (no text), for review

Usage:
  python export_clean.py [run_dir]                 # default: out
  python export_clean.py sample33
  python export_clean.py out --min-chars 2000      # extra length floor
  python export_clean.py out --include stub        # also keep a grade you trust

Re-grades from the actual .txt on disk (not the stored quality column), so it
works on folders fetched before quality-grading existed, and stays correct if
the rules improve.
"""
import argparse, csv, json, os, sys
from pathlib import Path

# reuse the exact grader the pipeline uses, so export and run agree
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_fulltext import classify_quality, _is_botwall


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", default="out",
                    help="Run folder containing _manifest.csv + pub.*.txt")
    ap.add_argument("--min-chars", type=int, default=1500,
                    help="Drop clean papers shorter than this (default 1500)")
    ap.add_argument("--include", nargs="*", default=[],
                    choices=["stub", "refs_only", "non_article"],
                    help="Also keep these grades (default: clean only)")
    args = ap.parse_args()

    base = Path(__file__).resolve().parent / args.run_dir
    manifest = base / "_manifest.csv"
    if not manifest.exists():
        sys.exit(f"no manifest at {manifest}")

    keep_grades = {"clean", *args.include}
    rows = list(csv.DictReader(manifest.open(encoding="utf-8")))

    kept, dropped = [], []
    corpus = (base / "clean_corpus.jsonl").open("w", encoding="utf-8")
    for r in rows:
        pid = r["pub_id"]
        if not r["status"].startswith("ok"):
            continue                                    # a miss, no file
        f = base / f"{pid}.txt"
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        grade = "botwall" if _is_botwall(text) else classify_quality(text, r["source"])
        reason = None
        if grade not in keep_grades:
            reason = grade
        elif len(text) < args.min_chars:
            reason = f"short(<{args.min_chars})"
        if reason:
            dropped.append((pid, r["publisher"], r["source"], len(text), reason))
            continue
        corpus.write(json.dumps({
            "pub_id": pid, "doi": r["doi"], "publisher": r["publisher"],
            "source": r["source"], "chars": len(text), "text": text,
        }, ensure_ascii=False) + "\n")
        kept.append(r)
    corpus.close()

    # clean_index.csv — the kept rows, minus the text, for eyeballing
    if kept:
        cols = [c for c in kept[0].keys()]
        with (base / "clean_index.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(kept)

    # report
    import collections
    print(f"{args.run_dir}/: {len(rows)} manifest rows")
    print(f"  KEPT  {len(kept)} clean papers -> clean_corpus.jsonl "
          f"({', '.join(sorted(keep_grades))}, >= {args.min_chars} chars)")
    print(f"  DROPPED {len(dropped)}:")
    for reason, c in collections.Counter(d[4] for d in dropped).most_common():
        print(f"     {reason:16} {c}")
    if dropped:
        print("  dropped detail (first 15):")
        for pid, pub, src, n, reason in dropped[:15]:
            print(f"     {pid}  [{src:13}] {n:>7}c  {reason:16} {pub[:22]}")


if __name__ == "__main__":
    main()

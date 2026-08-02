"""Split a run's clean corpus into one Parquet file PER PUBLISHER.

The downstream consumer (an embedder / AI / code) reads Parquet, and wants the
papers grouped by publisher — so 9 publishers -> 9 files. Each row is one
genuine full-text paper: {pub_id, doi, publisher, source, chars, text}.

Reads the run's clean_corpus.jsonl (produced by export_clean.py). If that file
isn't there yet, it's built first with the same defaults as export_clean.py, so
a single command works end to end.

Outputs (in <run_dir>/by_publisher/):
  elsevier.parquet, springer_nature.parquet, mdpi.parquet, ...  one per publisher
  _summary.csv                                                  rows per file

Usage:
  python split_by_publisher.py [run_dir]              # default: out
  python split_by_publisher.py out --min-chars 2000
  python split_by_publisher.py out --include stub
  python split_by_publisher.py out --raw             # skip the embedding scrub
"""
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_fulltext import export_clean_corpus


def slug(publisher: str) -> str:
    """'Oxford University Press (OUP)' -> 'oxford_university_press_oup'."""
    s = publisher.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "unknown"


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
    ap.add_argument("--raw", action="store_true",
                    help="Skip the embedding scrub (keep reference lists, URLs, "
                         "Elsevier metadata in the exported text).")
    args = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        sys.exit("need pandas + pyarrow: .venv/Scripts/python.exe -m pip install pandas pyarrow")

    base = Path(__file__).resolve().parent / args.run_dir
    if not (base / "_manifest.csv").exists():
        sys.exit(f"no manifest at {base / '_manifest.csv'}")

    # Always (re)build clean_corpus.jsonl so the split matches the current flags
    # (--min-chars / --include / --raw) instead of a stale earlier export.
    kept, _ = export_clean_corpus(base, args.min_chars, set(args.include),
                                  clean=not args.raw)
    corpus = base / "clean_corpus.jsonl"
    if not kept or not corpus.exists():
        sys.exit(f"no clean papers to split in {args.run_dir}/")

    # Group the corpus lines by publisher.
    groups: dict[str, list[dict]] = {}
    with corpus.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            groups.setdefault(rec.get("publisher") or "unknown", []).append(rec)

    out_dir = base / "by_publisher"
    out_dir.mkdir(exist_ok=True)
    # Clear stale parquet from a previous split (publishers/flags may have changed).
    for old in out_dir.glob("*.parquet"):
        old.unlink()

    cols = ["pub_id", "doi", "publisher", "source", "chars", "text"]
    summary = []
    for publisher, recs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        df = pd.DataFrame(recs, columns=cols)
        fname = slug(publisher) + ".parquet"
        df.to_parquet(out_dir / fname, engine="pyarrow", index=False)
        summary.append({"publisher": publisher, "file": fname, "papers": len(recs)})

    pd.DataFrame(summary).to_csv(out_dir / "_summary.csv", index=False)

    total = sum(s["papers"] for s in summary)
    print(f"{args.run_dir}/by_publisher/: {total} papers -> {len(summary)} parquet files")
    for s in summary:
        print(f"  {s['file']:34} {s['papers']:>5}")


if __name__ == "__main__":
    main()

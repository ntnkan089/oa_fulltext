"""Export only the CLEAN full-text papers from a run, ready for a downstream
embedding / RAG pipeline. Filters out the garbage the validator flags
(non_article / stub / refs_only / bot-wall) so you never embed a correction
notice or an abstract-stub.

The main fetcher already auto-writes clean_corpus.jsonl at the end of every run
(unless --no-export). Use this standalone script to (re)generate it for an
existing folder, or to tune --min-chars / --include without re-fetching.

Outputs in the run folder:
  clean_corpus.jsonl   {"pub_id","doi","publisher","source","chars","text"} per line
  clean_index.csv      the manifest rows that made the cut (no text), for review

Usage:
  python export_clean.py [run_dir]                 # default: out
  python export_clean.py sample33
  python export_clean.py out --min-chars 2000
  python export_clean.py out --include stub        # also keep a grade you trust
"""
import argparse, collections, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_fulltext import export_clean_corpus


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
    if not (base / "_manifest.csv").exists():
        sys.exit(f"no manifest at {base / '_manifest.csv'}")

    kept, dropped = export_clean_corpus(base, args.min_chars, set(args.include))
    keep = ", ".join(sorted({"clean", *args.include}))
    print(f"{args.run_dir}/: KEPT {kept} clean papers -> clean_corpus.jsonl "
          f"({keep}, >= {args.min_chars} chars)")
    print(f"  DROPPED {sum(dropped.values())}:")
    for reason, c in collections.Counter(dropped).most_common():
        print(f"     {reason:16} {c}")


if __name__ == "__main__":
    main()

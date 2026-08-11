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
    ap.add_argument("--prune-txt", action="store_true",
                    help="After the Parquet is written AND verified readable, "
                         "delete the raw pub.*.txt files whose paper is now safely "
                         "inside a Parquet, to reclaim disk. Resume is unaffected "
                         "(it is driven by _manifest.jsonl, not the .txt files). "
                         "Only the kept (clean) papers are deleted; garbage .txt "
                         "stay for audit. Destructive — you cannot re-export those "
                         "papers or change --min-chars/--raw for them afterward.")
    ap.add_argument("--prune-all-txt", action="store_true",
                    help="Like --prune-txt, but ALSO delete the garbage pub.*.txt "
                         "(stub / non_article / refs_only) that no Parquet keeps — "
                         "no audit trail left. Still verifies the clean papers "
                         "byte-for-byte first and deletes nothing if that fails.")
    args = ap.parse_args()
    if args.prune_all_txt:
        args.prune_txt = True

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
    used: dict[str, str] = {}  # filename -> publisher, to catch slug collisions
    for publisher, recs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        fname = slug(publisher) + ".parquet"
        # Two distinct publisher strings must never map to one file (silent
        # overwrite = lost papers). Disambiguate with a numeric suffix.
        if fname in used and used[fname] != publisher:
            i, base_slug = 2, slug(publisher)
            while f"{base_slug}_{i}.parquet" in used:
                i += 1
            fname = f"{base_slug}_{i}.parquet"
        used[fname] = publisher
        df = pd.DataFrame(recs, columns=cols)
        df.to_parquet(out_dir / fname, engine="pyarrow", index=False,
                      compression="zstd")
        summary.append({"publisher": publisher, "file": fname, "papers": len(recs)})

    pd.DataFrame(summary).to_csv(out_dir / "_summary.csv", index=False)

    total = sum(s["papers"] for s in summary)
    print(f"{args.run_dir}/by_publisher/: {total} papers -> {len(summary)} parquet files")
    for s in summary:
        print(f"  {s['file']:34} {s['papers']:>5}")

    if args.prune_txt:
        # VERIFY-then-DELETE. Re-open every parquet we just wrote and compare each
        # paper's text BYTE-FOR-BYTE against the exact text we put in it. A .txt is
        # deleted only when its paper is provably identical inside a readable
        # parquet; if a single paper fails to verify, abort and delete nothing.
        # (~9 ms/paper — a few minutes even at 50k, negligible vs. the fetch.)
        expected = {r["pub_id"]: r["text"] for recs in groups.values() for r in recs}
        verified: set[str] = set()
        for s in summary:
            back = pd.read_parquet(out_dir / s["file"], columns=["pub_id", "text"])
            for pid, txt in zip(back["pub_id"], back["text"]):
                if isinstance(txt, str) and txt == expected.get(pid):
                    verified.add(pid)
        if verified != set(expected):
            missing = len(set(expected) - verified)
            sys.exit(f"prune aborted: {missing} paper(s) did not verify "
                     f"byte-for-byte — no .txt deleted")
        freed = deleted = 0
        for pid in verified:
            f = base / f"{pid}.txt"
            if f.exists():
                freed += f.stat().st_size
                f.unlink()
                deleted += 1
        print(f"--prune-txt: verified all {len(verified)} papers byte-for-byte; "
              f"deleted {deleted} raw .txt, freed {freed/1e6:.1f} MB")
        if args.prune_all_txt:
            # Only reached once the clean papers verified above — so this deletes
            # the leftover garbage (stub / non_article / refs_only) that no Parquet
            # kept. No audit trail remains after this.
            g_freed = g_deleted = 0
            for f in base.glob("pub.*.txt"):
                g_freed += f.stat().st_size
                f.unlink()
                g_deleted += 1
            print(f"--prune-all-txt: also deleted {g_deleted} garbage .txt, "
                  f"freed {g_freed/1e6:.1f} MB (no audit trail left)")


if __name__ == "__main__":
    main()

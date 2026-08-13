# Open-Access Full-Text Downloader

Give it a list of DOIs; it retrieves the open-access full text of each paper,
keeps only the genuine full-text ones, and writes each as its own **Parquet file
inside a publisher-named folder** — ready for a downstream embedding / AI pipeline.

Built for the `oa_selected_pub` sample (20,000 papers, 9 publishers, 4 OA types).

## Quick start

```bash
git clone https://github.com/ntnkan089/oa_fulltext.git
cd oa_fulltext
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

Optional publisher keys (free — Elsevier, Springer, Wiley) go in a `.env` file
(copy `.env.example` to `.env`). They're loaded automatically and `.env` is
git-ignored. The pipeline runs without them; a key just recovers more papers for
that publisher.

## Run it (two commands)

```bash
# 1. fetch the full text
python fetch_fulltext.py --sample 50 --workers 12 --miss-retries 2 --paper-timeout 90

# 2. build the clean per-publisher Parquet, then delete the raw text files
python split_by_publisher.py out --prune-all-txt
```

That's it. `out/by_publisher/` now holds one folder per publisher, and inside each
folder one Parquet file per paper:

```
out/by_publisher/
    Elsevier/pub.123.parquet
    Elsevier/pub.456.parquet
    Springer Nature/pub.789.parquet
    ...
    _summary.csv
```

Each `pub.<id>.parquet` is a single genuine full-text paper:
`{pub_id, doi, publisher, source, chars, text}`. (`--prune-all-txt` verifies every
paper is safely inside its Parquet, byte-for-byte, before deleting anything.)

**Choosing what to fetch:**

| flag | meaning |
|------|---------|
| `--sample 50` | ~50 papers **per publisher**, spread across OA types (balanced test) |
| `--limit 350` | the first 350 rows of the sheet as-is |
| `--ids pub.123 pub.456` | specific publication IDs |
| `--input my.csv` | your own CSV (needs `Publication ID` + `DOI` columns) |
| `--out FOLDER` | where to write (default `out`) |

`--workers N` fetches N papers at once (12 is the sweet spot). Re-running
**resumes** automatically; `--restart` starts fresh.

**Want to keep the raw text too?** Use a gentler step 2:

```bash
python split_by_publisher.py out                 # keep every .txt, just add the Parquet
python split_by_publisher.py out --prune-txt     # delete only the clean .txt, keep the few "garbage" ones for review
```

## How it works

**One code path for all 9 publishers**, not nine scrapers. Each DOI is resolved
through provider-neutral sources, best-quality-first, keeping the longest result:

1. **Publisher APIs** (Elsevier / Springer / Wiley, if a key is set) — cleanest text.
2. **Europe PMC / NCBI PMC** — JATS XML full text (clean sections + paragraphs).
3. **Unpaywall PDFs** — extracted with PyMuPDF.
4. **Landing pages** — article body + any PDF the page advertises.
5. **`doi.org` fallback** and optional **headless browser** (`--use-browser`).

**Every hit is auto-graded** so garbage never reaches your corpus:

- `clean` — genuine full-text body (kept)
- `non_article` — correction / erratum / editorial (no body exists)
- `refs_only` — got the abstract + bibliography but not the body
- `stub` — short abstract-only
- bot-wall pages (Cloudflare "just a moment") are detected and rejected

`split_by_publisher.py` keeps only `clean` and drops the rest. Each kept paper is
also scrubbed for embedding (trailing reference list, stray URLs, and Elsevier
metadata removed). Pass `--raw` to skip the scrub, `--min-chars N` to change the
length floor, `--include stub` to also keep a grade you trust.

## Output

| file | what |
|------|------|
| `out/pub.<ID>.txt` | one plain-text file per retrieved paper (working format) |
| `out/_manifest.csv` | one row per DOI: status, source, char count, `quality` grade |
| `out/_manifest.jsonl` | append-only checkpoint (drives resume) |
| `out/by_publisher/<Publisher>/pub.<ID>.parquet` | **the deliverable** — one clean full-text paper per file, filed under its publisher |
| `out/by_publisher/_summary.csv` | paper counts per publisher |

## Result (270-paper test, 30 per publisher)

**With Elsevier + Springer keys: 209/270 (77%)** genuine full text in ~25 min.
Key-free baseline: **187/270 (69%)**. On the UCI VPN (institutional IP) it rises
to ~77% as entitlement-gated papers (Oxford UP, etc.) unlock. The remaining tail
is Cloudflare-walled publisher pages with no PMC mirror and a few non-article
DOIs — structurally gated, not a pipeline gap. Projected on the full 20k: ~77%
with the Elsevier key. See `NOTES.md` for the full breakdown.

## Files

| file | what it does |
|------|--------------|
| `fetch_fulltext.py` | the scraper: DOI resolver chain + downloader + manifest + grading |
| `split_by_publisher.py` | clean corpus → one Parquet per paper, filed under its publisher (the deliverable) |
| `export_clean.py` | build just the `clean_corpus.jsonl` (single-file variant) |
| `audit_quality.py` | re-run the quality grading over an existing folder |
| `requirements.txt` | Python dependencies |
| `.env.example` | template for optional publisher API keys |
| `NOTES.md` | design notes, practicability findings, next steps |

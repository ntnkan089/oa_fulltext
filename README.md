# Open-Access Full-Text Downloader — prototype

Give it a list of DOIs (with Publication IDs); it writes the open-access full
text of each paper to `out/pub.<ID>.txt`. Built to test the practicability of
bulk-retrieving full text for the `oa_selected_pub` sample (20k papers, 9
publishers, 4 OA types).

## Answering the spec's questions

**API or web scraping?** Both, layered. We don't scrape publisher sites
directly first. Instead each DOI is run through provider-neutral **APIs** that
already know where the free copy lives (Europe PMC, NCBI PMC, Unpaywall), and
we only fall back to fetching/parsing the publisher landing page when those
don't resolve. This is why one code path covers all 9 publishers.

**One code or one-per-publisher?** **One code path**, not nine. The OA copy is
located by DOI through the shared resolver chain below, so publisher-specific
scraping is mostly unnecessary. The exceptions are publishers that hide their
OA full text behind JavaScript or a Cloudflare bot-wall (Elsevier, and
sometimes MDPI/SAGE) — those need a publisher API key or a headless browser,
added as *one more resolver*, not a separate program. The `Publisher` column
is recorded but not currently required as input.

## How a DOI is resolved

Sources are tried best-text-quality-first, and we keep the **longest** result
("best of locations") so a repository abstract never wins over a real
full-text copy that also exists. We stop early the moment a source clears a
confident-full-text bar (`CONFIDENT_CHARS`, 6000); otherwise every source is
tried and the longest result ≥ `--min-chars` (default 1000) wins.

0. **Publisher APIs (optional, need a key)** — Elsevier Article Retrieval,
   Springer Nature OA, and Wiley TDM, used only for those publishers' DOIs.
   Cleanest output, the fix for ScienceDirect's JS wall, and (Wiley) entitled
   PDFs by DOI past the publisher wall.
1. **Europe PMC JATS XML** — papers in EPMC's open-access subset.
2. **NCBI PMC** — if a PMCID is known (from EPMC, an Unpaywall PMC link, or a
   last-resort title search): `efetch` JATS XML, then the PMC HTML page.
   Recovers most "green" OA copies.
3. **Unpaywall PDFs** — every OA location's PDF, extracted with PyMuPDF. If a
   browser UA gets HTML instead of a PDF (some repositories serve bots only),
   we retry with a bare UA.
4. **Landing pages** — the article body via trafilatura, AND any PDF the page
   advertises (`citation_pdf_url`, `rel=alternate`, DSpace `/bitstream` links)
   — this rescues repository deposits that show only an abstract as HTML.
5. **Headless browser (optional, `--use-browser`)** — Playwright renders
   Cloudflare/JS publisher pages (MDPI/SAGE/Elsevier) as a last resort.

## Install

Needs Python 3.10+.

```bash
git clone https://github.com/ntnkan089/oa_fulltext.git
cd oa_fulltext
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

Optional publisher keys go in a `.env` file (copy `.env.example` to `.env` and
paste your keys) so you don't pass them on every run — they're loaded
automatically. `.env` is git-ignored, so your keys never get committed.

## Run

```
# practicability test: ~2 papers PER publisher, spread across OA types
python fetch_fulltext.py --sample 2

# specific publication ids
python fetch_fulltext.py --ids pub.1192432907 pub.1191002603

# a real batch from the sheet (first N rows), 12 papers at a time
python fetch_fulltext.py --limit 100 --workers 12

# custom DOI list (any CSV with 'Publication ID' + 'DOI' columns)
python fetch_fulltext.py --input my_dois.csv

# with publisher keys (cleaner text + recovers ScienceDirect)
python fetch_fulltext.py --sample 30 --elsevier-key XXX --springer-key YYY
#   (or set $ELSEVIER_API_KEY / $SPRINGER_API_KEY)

# add the headless-browser fallback for the Cloudflare/JS tail
pip install playwright && python -m playwright install chromium
python fetch_fulltext.py --sample 30 --use-browser
```

Keys: Elsevier (free, https://dev.elsevier.com), Springer Nature (free,
https://dev.springernature.com). Both are optional — the pipeline runs key-free
and only uses a key for that publisher's DOIs.

**Concurrency.** `--workers N` (default 8) fetches N papers at once — the main
lever for a full-scale run (270 papers ≈ 25 min serial → a few minutes at
`--workers 12`; the 20k corpus goes from ~30 h to a few hours). Each paper writes
its own file + manifest line, so parallel workers never collide, and resume works
unchanged. `--use-browser` forces `--workers 1` (Playwright is single-threaded).

**Bot-wall guard.** Some repositories/publishers (EconStor's Anubis, Cloudflare
"Just a moment") serve a ~1 KB "prove you're human" page that would otherwise
clear `--min-chars` and be saved as if it were the paper. Those are detected and
recorded as `oa_blocked`, not counted as hits.

**Quality grading (built in).** Every hit is auto-graded into a `quality` column
in the manifest — `clean` (genuine full text), `non_article` (correction / erratum
/ editorial — no body exists), `refs_only` (landing scrape that got abstract +
bibliography but not the body), or `stub` (short abstract-only). The run summary
prints both the raw retrieved count and the honest "genuine full text" count, so
the hit rate isn't inflated by non-papers. `python audit_quality.py [out_dir ...]`
is the same logic as a standalone re-check over existing folders.

Re-running **resumes** automatically (skips Publication IDs already fetched OK,
per `out/_manifest.jsonl`); pass `--restart` to start fresh. `--input` defaults
to the `oa_selected_pub - Sheet1.csv` in Downloads.

**Built for scale (20k):** resume also **caches misses** — papers already
recorded `oa_blocked` / `not_indexed` are skipped on restart (pass
`--retry-misses` to re-attempt), so a big run doesn't re-hit thousands of dead
DOIs, each costing several 45 s timeouts.

**Recovering the tail over time:** many misses are just *"not in PMC yet"* —
MDPI/SAGE/Frontiers deposit to PMC on a lag. Run `--recheck-misses` weeks later
to re-attempt only the recorded misses (ignores `--sample`/`--limit`) and report
how many PMC has since indexed. It's also the way to sweep up the tail after
turning on the VPN or adding a key. The manifest CSV is deduped newest-wins, so
recovered papers cleanly replace their old miss row. `--paper-timeout N` (default 120 s) is a
soft per-paper budget that stops trying more sources once exceeded, so one hung
server can't stall a worker. The `get()` helper honors `429`/`503` `Retry-After`
so a fast run backs off instead of getting the polite-pool APIs to block it.

## Output

| file | what |
|------|------|
| `out/pub.<ID>.txt` | one plain-text file per retrieved paper |
| `out/_manifest.csv` | one row per attempted DOI: status, source used, char count, `quality` grade, source URL |
| `out/_manifest.jsonl` | append-only checkpoint (drives resume) |

`status` is one of: `ok`, `ok_thin` (got text but it's short scraped HTML —
likely an abstract/repository stub, not full text), `oa_blocked` (an OA copy
exists but the publisher blocked the download / it parsed too short),
`not_indexed` (no free copy known — likely truly closed / a non-article DOI),
`error`.

## Practicability result (270-paper sample, 30 per publisher)

**Recommended config — Elsevier + Springer keys, browser OFF: 209/270 (77%)**
in ~25 min. The Elsevier key is the big lever (Elsevier 4→27/30); Frontiers
30/30, Springer 27/30. The headless-browser fallback was tested and does NOT
beat the Cloudflare/Akamai walls (MDPI `Access Denied`, SAGE stuck on the
challenge), so leave it off for bulk runs — it costs hours and recovers almost
nothing. The remaining ~60 misses are Cloudflare/no-PMC publisher pages plus a
few non-article DOIs.

Key-free baseline (no keys): **187/270 (69%) — 182 real full text + 5 thin.** XML
for most (NCBI PMC), PDF/HTML for the rest. Per publisher (full text):
Frontiers 29, CUP 28, Springer 27, SAGE 24, Wiley 21, OUP 17, MDPI 16, T&F 16,
**Elsevier 4**. By OA type: Gold 79%, Green 62%, Hybrid 60%, Bronze 46%.

The key-free pipeline is now maxed out: the repository-PDF scraping, bot-UA
retry, and best-of-locations logic mostly converted thin abstracts into real
full text rather than raising the raw count. The remaining 83 misses are
structurally gated, not pipeline gaps:

- **Elsevier (26)** — ScienceDirect JS wall, no green copy → **`--elsevier-key`**.
- **MDPI/OUP/T&F/Wiley/SAGE (53)** — Cloudflare/JS publisher pages with no PMC
  or repository mirror → **`--use-browser`** (Playwright) or that publisher API.
- **CUP/Frontiers/Springer (4)** — non-article DOIs / genuinely closed.

So the two levers that actually raise the number from here are the Elsevier key
and the browser fallback (both implemented). The misses below are `oa_blocked`:

- **Elsevier** — ScienceDirect serves a JavaScript shell to scrapers and these
  papers had no PMC mirror. Elsevier is ~26% of the full sample, so this is the
  one slice worth an **API key** (free [ScienceDirect/TDM API](https://dev.elsevier.com));
  add it as resolver #0 in `resolve_fulltext()`.
- A few publisher landing pages (some MDPI/SAGE/T&F) sit behind Cloudflare and
  401/403 plain HTTP. When they have a PMC copy we already get them; when they
  don't, a headless browser (Playwright) would be the fallback.

See `NOTES.md` for details and next steps.

## Adding a publisher API later

Drop one more attempt into `resolve_fulltext()` following the existing
`(text, source_label, url, reason)` contract — nothing else changes. Good first
candidate: Elsevier ScienceDirect (biggest slice, cleanest keyed output).

## Files

| file | what it does |
| --- | --- |
| `fetch_fulltext.py` | the whole tool: DOI resolver chain + downloader + manifest |
| `requirements.txt` | Python dependencies |
| `.env.example` | template for optional publisher API keys (copy to `.env`) |
| `NOTES.md` | design notes, practicability findings, and next steps |

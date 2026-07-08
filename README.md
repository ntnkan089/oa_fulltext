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

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Run

```
# practicability test: ~2 papers PER publisher, spread across OA types
python fetch_fulltext.py --sample 2

# specific publication ids
python fetch_fulltext.py --ids pub.1192432907 pub.1191002603

# a real batch from the sheet (first N rows)
python fetch_fulltext.py --limit 100

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

Re-running **resumes** automatically (skips Publication IDs already fetched OK,
per `out/_manifest.jsonl`); pass `--restart` to start fresh. `--input` defaults
to the `oa_selected_pub - Sheet1.csv` in Downloads.

## Output

| file | what |
|------|------|
| `out/pub.<ID>.txt` | one plain-text file per retrieved paper |
| `out/_manifest.csv` | one row per attempted DOI: status, which source was used, char count, source URL |
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

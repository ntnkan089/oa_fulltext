# OA Full-Text Downloader — prototype notes

Prototype status as of 2026-06-24. Built against `Instruction.pdf` and the
`oa_selected_pub` sample (20,000 rows).

## The sample (what we're up against)

Publishers: Elsevier 5216, MDPI 4323, Springer Nature 3644, Wiley 2583,
Frontiers 1617, Oxford UP 1189, Taylor & Francis 716, SAGE 522, Cambridge UP
190. OA types: Gold 11019, Hybrid 4506, Bronze 2523, Green 1944 (+8 "Closed").

## What works

- **Single code path, all 9 publishers** — DOIs resolved via Europe PMC →
  NCBI PMC → Unpaywall PDF → landing HTML, best-quality-first
  (`resolve_fulltext`). No per-publisher scrapers.
- **Clean text where possible** — JATS XML (Europe PMC / NCBI efetch) gives
  real sections + paragraphs. PDF (PyMuPDF) and HTML (trafilatura) are the
  lower-quality fallbacks.
- **Honest hit-rate reporting** — misses are classified `oa_blocked` vs
  `not_indexed`, so the manifest shows *why* a paper failed, not just that it
  did. This is the point of the prototype.
- **Resume / checkpoint** — `_manifest.jsonl` is append-only; re-running skips
  done IDs. Safe against crashes / Ctrl-C / network blips.
- **Polite** — browser-like UA with contact mailto, `--delay` between papers,
  one retry on transient errors.

## Practicability findings (270-paper sample, 30 per publisher)

Key-free, after the round-2 improvements below:

| | result |
|---|---|
| Retrieved key-free | **187/270 (69%)** — 182 full text + 5 thin |
| Source: NCBI PMC XML (clean) | 119 |
| Source: Unpaywall PDF | 34 |
| Source: landing HTML | 24 |
| Source: NCBI PMC HTML | 5 |
| Miss: `oa_blocked` | 82 |
| Miss: `not_indexed` | 1 |

By publisher (full text/30): Frontiers 29, CUP 28, Springer 27, SAGE 24,
Wiley 21, OUP 17, MDPI 16, T&F 16, **Elsevier 4**.

By OA type: Gold 79%, Green 62%, Hybrid 60%, Bronze 46%.

Where the 83 misses are: Elsevier 26, MDPI 13, OUP 13, T&F 13, Wiley 9,
SAGE 5, CUP 2, Frontiers 1, Springer 1.

**The key-free path is now maxed out** — round-2 fixes mostly turned thin
abstracts into real full text rather than raising the raw count. The remaining
misses are structurally gated: Elsevier (26) needs its API key; the
MDPI/OUP/T&F/Wiley/SAGE tail (53) is Cloudflare/JS publisher pages with no PMC
or repository copy, needing `--use-browser` or a publisher API; ~4 are
non-articles / closed.

### With keys (Elsevier + Springer), no browser — measured

**209/270 (77%)** — 204 full text + 5 thin. Elsevier jumped 4→27/30 (the key),
Frontiers 30/30, Springer 27/30 (18 now via clean API XML). Source mix: 108 PMC
XML, 31 PDF, 26 Elsevier API, 18 Springer API, 20 landing, 6 PMC HTML. Misses
down to 60 `oa_blocked` + 1 `not_indexed`.

Run time ~25 min (browser OFF). The browser fallback was tested and does NOT
beat MDPI/SAGE (see "Browser fallback — reality check"), so the fast keyed run
is the recommended config — there's no real speed/coverage tradeoff.

Projection on the full 20k: ~68-70% key-free; **~77% with the Elsevier key**
(low-80s if Elsevier entitlements are fuller from a UCI campus IP). The
remaining tail is Cloudflare/no-PMC publisher pages + non-article DOIs.

NOTE: misses fan out across many sources, so a stubborn paper can take a while
(several 45s timeouts). The 270 run takes ~25-30 min; full-text papers are fast
(they stop at the first confident hit). Tune timeouts/parallelism for 20k-scale.

### The two hard cases

1. **Elsevier (ScienceDirect)** — `is_oa=True` in Unpaywall but only a
   `doi.org` landing URL that resolves to a JS-rendered ScienceDirect shell
   (~2.6 KB of HTML, no body). No PMC mirror for these. **Fix: Elsevier TDM/
   ScienceDirect API key** (free, instant-ish at dev.elsevier.com) returns the
   article as XML/JSON by DOI. Add as resolver #0. Biggest payoff — Elsevier is
   ~26% of the sample.
2. **Cloudflare bot-walls** — MDPI and SAGE landing pages 403 plain HTTP even
   with a browser UA. We currently dodge this when a PMC copy exists (worked
   for both sampled MDPI + SAGE papers). For ones with no PMC copy, options are
   a headless browser (Playwright) or just accepting the miss.

## Improvements added (2026-06-24, round 2)

- **Best-of-locations** — resolver now collects candidates from all sources and
  keeps the longest (stops early only on a confident ≥6k-char hit). A
  repository abstract no longer wins over a real full-text copy.
- **Repository PDF scraping** — landing pages are mined for the real PDF link
  (`citation_pdf_url`, `rel=alternate`, DSpace `/bitstream`, `.pdf` hrefs).
  Converted many thin abstract pages into full PDFs (e.g. one OUP 1.1k→67k).
- **Bot-UA retry** — some repos (EconStor) serve the PDF only to a bare UA and
  a JS interstitial to a browser UA; we now retry PDF downloads with a plain UA.
  Recovered green Elsevier/MDPI copies with no key.
- **Elsevier + Springer API resolvers** — `--elsevier-key` / `--springer-key`
  (or env). Cleanest text for ~44% of the corpus; fixes ScienceDirect's JS wall.
- **Title-based PMC fallback** — last-resort PMC lookup by title (strict match)
  for DOIs Unpaywall 404s. (Note: social-science papers aren't in PMC, so this
  only helps biomed-ish content.)
- **Optional Playwright fallback** (`--use-browser`) for the Cloudflare/JS tail.

## Browser fallback — reality check (2026-06-24)

Tested headless Chromium (Playwright) against the hard publishers directly:

- **MDPI** → `Access Denied` (server-level block of headless/datacenter
  browsers; not even a Cloudflare challenge). Headless does NOT get in.
- **SAGE** → stuck on Cloudflare's `Just a moment...` interstitial; headless
  fails the bot fingerprint and never resolves.

So `--use-browser` only helps *softer* JS/landing pages (it recovered a couple
of CUP/OUP-type pages), **not** the Cloudflare/Akamai-hardened MDPI/SAGE. Beating
those would need stealth-browser/residential-proxy tricks — fragile and
ToS-dubious. Conclusion: for MDPI/SAGE the only reliable free route is **PMC**
(already used); their landing pages are effectively unscrapable. The browser was
refactored to reuse ONE Chromium for the whole run (was launching one per page →
hours; now seconds per render), so it's cheap to leave on for the soft cases.

## Free aggregators are exhausted (tested 2026-06-24)

Probed the 61 keyed-run misses against OpenAlex / Crossref TDM links / Semantic
Scholar. OpenAlex returned a candidate URL for **58/61** — but when actually
downloaded, only **1/61** yielded text. The candidates are the same walled
publisher pages Unpaywall already gave us. **Conclusion: adding more free OA
aggregators (OpenAlex, CORE, S2, Crossref) recovers ~nothing** — the remaining
misses have no fetchable free copy anywhere. The only stable lever left is
institutional/entitled access:

- **Wiley TDM API** — added (`--wiley-token` / `$WILEY_TDM_TOKEN`). One clean
  keyed API; returns entitled Wiley PDFs by DOI past the wall. Targets the ~9
  Wiley misses. (Couldn't measure here — needs the UCI token.)
- **UCI EZproxy / Shibboleth** — would recover OUP/T&F/SAGE subscription content
  (~29 papers) but is a bigger lift (login flows). Documented, not built.

## Remaining hard tail (what still misses)

- **Repository-only social-science deposits** (hal.science, Pure portals, some
  EconStor) where the PDF is JS-gated and there's no PMC copy — flagged
  `ok_thin`. Needs `--use-browser` or per-repo handling.
- **Cloudflare-walled bronze/hybrid with no second copy** — needs `--use-browser`
  or the publisher API.
- **Non-article DOIs** (meeting abstracts, errata, editorials) — no full text
  exists; unfixable.

## Possible next steps

1. Re-run `--sample 30` to confirm the new key-free hit rate (in progress).
2. Try `--use-browser` on the residual `ok_thin` / `oa_blocked` set to see how
   much the Cloudflare tail is worth.
3. PDF cleanup is minimal (de-hyphenation + whitespace). If downstream needs
   cleaner PDFs (strip running headers/page numbers/refs), add a pass — XML/API
   sources already avoid this.

## Files that ever need editing

- `resolve_fulltext()` — add/reorder sources (e.g. a publisher API).
- `jats_to_text()` — what XML elements to keep/drop (currently drops refs,
  tables, figures, formulas).
- `COL_*` constants / `--col-id` / `--col-doi` — if the input sheet's headers
  change.

## Not done / out of scope for the prototype

- Did **not** fetch all 20k (per the spec — this only tests practicability).
- No publisher API keys yet (measuring the key-free baseline first).
- No headless-browser fallback for Cloudflare-walled pages.

# Setup — full test

Steps to exercise every retrieval path. PowerShell commands, run from
`C:\Users\ntnka\aih-dev\oa_fulltext`. The `.venv` and dependencies are already
installed; step 1 is only for a fresh machine.

## 1. Environment (already done here)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Smoke test (no keys, no browser — should retrieve ~14/18):

```powershell
.venv\Scripts\python fetch_fulltext.py --sample 2 --restart
```

Results land in `out\` — one `pub.<ID>.txt` per paper, plus `out\_manifest.csv`
(open in Excel) showing status / source / char count for every DOI.

## 2. Publisher API keys (for the Elsevier + Springer slices, ~44% of corpus)

Both are **free**. They give clean structured text and fix ScienceDirect.

**Elsevier** — https://dev.elsevier.com → sign in (free Elsevier account) →
"Create API Key" → accept the TDM terms. Copy the key.
- Caveat: for some content Elsevier only returns full text from a registered
  **institutional IP**; open-access articles generally work with the key alone.
  If you're on a university network, run it there for best coverage.

**Springer Nature** — https://dev.springernature.com → register → create an app
→ use the key for the **Open Access** API.

**Wiley (TDM token)** — institutional, tied to UCI's subscription. Request a
**Wiley-TDM-Client-Token** via the Wiley Online Library text-and-data-mining
page (https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining)
or through **UCI Libraries**. Recovers Wiley papers that have no free copy
(subscription content you're entitled to). Put it in `.env` as
`WILEY_TDM_TOKEN=...`. Optional — skip it and Wiley still gets ~70% via PMC/PDF.

Pass them either as flags or environment variables:

```powershell
# flags
.venv\Scripts\python fetch_fulltext.py --sample 5 --restart `
    --elsevier-key YOUR_ELSEVIER_KEY --springer-key YOUR_SPRINGER_KEY

# or env vars (persist for the session)
$env:ELSEVIER_API_KEY = "YOUR_ELSEVIER_KEY"
$env:SPRINGER_API_KEY = "YOUR_SPRINGER_KEY"
.venv\Scripts\python fetch_fulltext.py --sample 5 --restart
```

The banner prints `Keys: Elsevier on, Springer on` so you can confirm they're picked up.

## 3. Headless-browser fallback (for the Cloudflare/JS tail: MDPI, SAGE, Elsevier pages)

One-time install of Playwright + a Chromium build (~150 MB):

```powershell
.venv\Scripts\python -m pip install playwright
.venv\Scripts\python -m playwright install chromium
```

Then add `--use-browser` to any run:

```powershell
.venv\Scripts\python fetch_fulltext.py --sample 5 --restart --use-browser
```

It's used only as a last resort (slow), so it won't slow down papers that
resolve via API/PMC/PDF.

## 4. The full test (everything on)

```powershell
$env:ELSEVIER_API_KEY = "YOUR_ELSEVIER_KEY"
$env:SPRINGER_API_KEY = "YOUR_SPRINGER_KEY"
.venv\Scripts\python fetch_fulltext.py --sample 30 --restart --use-browser
```

~270 papers, 30 per publisher, spread across OA types. Expect ~25-40 min
(misses fan out across sources; with `--use-browser` they're slower still). The
end-of-run SUMMARY shows the hit rate, source mix, and per-publisher breakdown;
`out\_manifest.csv` has the per-DOI detail.

Tip: `--restart` wipes the manifest and starts fresh. Omit it to **resume** —
re-running skips papers already retrieved (handy after a crash / Ctrl-C, or to
re-try only the misses).

## 5. Reading the results

- `out\pub.<ID>.txt` — the full text files.
- `out\_manifest.csv` — columns: `pub_id, doi, publisher, oa_type, status,
  source, url, chars, note`. Sort by `status` to see misses; by `chars` to spot
  thin extractions.
- `status` values: `ok` (full text), `ok_thin` (short HTML, likely abstract),
  `oa_blocked` (OA exists but download blocked), `not_indexed` (no free copy
  found), `error`.

"""Open-access full-text downloader — prototype.

Given a list of DOIs (with Publication ID, and optionally Publisher / Open
Access type), resolve where the open-access full text lives, download it, and
write one plain-text file per paper named after its Publication ID:

    out/pub.1192432907.txt

Usage:
    # practicability test: stratified sample across all publishers
    python fetch_fulltext.py --sample 3

    # specific publication ids
    python fetch_fulltext.py --ids pub.1192432907 pub.1191002603

    # a custom DOI list (CSV with 'Publication ID' + 'DOI' columns)
    python fetch_fulltext.py --input my_dois.csv --limit 50

Outputs (in --out, default ./out):
    pub.<ID>.txt          one per paper that yielded text
    _manifest.csv         one row per attempted DOI: which source was used,
                          status, char count, and the URL it came from
    _manifest.jsonl       append-only checkpoint (drives --resume)

============================================================================
HOW IT WORKS  (read this first)
============================================================================
We are told these are *open-access* papers, so no login/fee should be needed.
The hard part is that "open access" hides several different hosting setups
(Gold / Hybrid / Bronze / Green) across 9 publishers. Rather than write a
scraper per publisher, we resolve each DOI through provider-neutral services
that already know where the free copy is, and only fall back to the publisher
page as a last resort. Sources are tried best-text-quality-first:

  1. EUROPE PMC (clean JATS XML).  Many OA papers are mirrored in Europe PMC
     with machine-readable full text. This is the *cleanest* source — real
     sections and paragraphs, no PDF-extraction garbage — so we try it first.

  2. UNPAYWALL best OA PDF.  Unpaywall (https://unpaywall.org) maps a DOI to
     its best free copy anywhere (publisher site, repository, PMC...). If that
     copy is a PDF we download it and extract text with PyMuPDF.

  3. UNPAYWALL / publisher landing HTML.  If the best copy is an HTML article
     page, we extract the article body with trafilatura.

If none of these yield enough text the DOI is recorded as a miss in the
manifest (with the reason) so the run reports a true hit rate — the whole
point of this prototype is to measure how practical bulk retrieval is.

WHY NO PUBLISHER API KEYS (yet).  Elsevier / Springer / Wiley offer keyed
text-mining APIs that return cleaner structured text, but they need per-
publisher registration. We start key-free to measure the baseline hit rate
and text quality across all 9 publishers with a single code path. If a big
slice (e.g. Elsevier, ~26% of the sample) extracts poorly from PDF, add its
API as another resolver in resolve_fulltext() — the rest of the pipeline
doesn't change. See README.md.
============================================================================
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# UTF-8 stdout on Windows so titles with accents/em-dashes don't crash prints.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dataclasses import dataclass, asdict
from pathlib import Path

import requests

DEFAULT_INPUT = r"C:\Users\ntnka\Downloads\oa_selected_pub - Sheet1.csv"
DEFAULT_EMAIL = "ntnkan089@gmail.com"   # Unpaywall / Europe PMC polite-pool contact

# Column names in the oa_selected_pub sheet. Override with --col-* if a future
# input uses different headers.
COL_ID = "Publication ID"
COL_DOI = "DOI"
COL_PUBLISHER = "Publisher"
COL_OA = "Open Access"
COL_TITLE = "Title"      # used only for the title-based PMC fallback

# Below this many characters we treat an extraction as "didn't really get the
# body" (e.g. we only scraped an abstract / cookie wall) and keep trying other
# sources. Tunable with --min-chars.
DEFAULT_MIN_CHARS = 1000

# A landing-page (HTML) extraction shorter than this is probably just an
# abstract / repository stub, not full text — common for green-OA copies hosted
# on institutional repositories (hdl.handle.net, hal.science, Pure portals)
# that show only metadata to a scraper. We still keep the file but flag it
# `ok_thin` so the manifest doesn't overstate the true full-text hit rate.
# (Only applied to scraped HTML; short PDF/XML can be a legitimately short paper.)
THIN_LANDING_CHARS = 2500

EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_FULLTEXT = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{pid}/fullTextXML"
)
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
# NCBI PMC: efetch returns clean JATS XML for the PMC OA subset; the article
# HTML page is the fallback for PMC records EuropePMC won't serve as XML.
NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
NCBI_PMC_HTML = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
PMCID_RE = re.compile(r"PMC\d+", re.I)

# Elsevier Article Retrieval API. With a free key (https://dev.elsevier.com) it
# returns OA full text by DOI — the fix for ScienceDirect's JS wall, which is
# the single biggest miss bucket. Key via --elsevier-key or $ELSEVIER_API_KEY.
ELSEVIER_ARTICLE = "https://api.elsevier.com/content/article/doi/{doi}"
# Springer Nature OpenAccess API returns JATS full text for OA articles by DOI.
# Free key at https://dev.springernature.com. Key via --springer-key /
# $SPRINGER_API_KEY.
SPRINGER_OA = "https://api.springernature.com/openaccess/jats"
# Wiley Text & Data Mining API returns the article PDF by DOI for content the
# requesting institution is entitled to (UCI subscribes). Token is an
# institutional "Wiley-TDM-Client-Token" from https://onlinelibrary.wiley.com/
# library-info/resources/text-and-datamining . Via --wiley-token / $WILEY_TDM_TOKEN.
WILEY_TDM = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}"

# If a source yields at least this many characters we treat it as confident
# full text and stop. Below it (but above --min-chars) we keep trying other
# sources/locations and return the LONGEST result — so a repository abstract
# never wins over a real full-text copy that also exists ("best of locations").
CONFIDENT_CHARS = 6000


@dataclass
class Result:
    """One attempted DOI, for the manifest / hit-rate report."""
    pub_id: str
    doi: str
    publisher: str = ""
    oa_type: str = ""
    status: str = ""        # ok | no_oa_location | download_failed | extract_empty | error
    source: str = ""        # europepmc_xml | unpaywall_pdf | landing_html | ""
    url: str = ""           # where the text actually came from
    chars: int = 0          # length of extracted text
    quality: str = ""       # for hits: clean | stub | refs_only | non_article
    note: str = ""          # error detail when status != ok


# ---------- HTTP ----------

def make_session(email: str) -> requests.Session:
    s = requests.Session()
    # Many publisher CDNs 403 a bare python-requests UA, so present as a normal
    # browser but keep the contact mailto the APIs ask for. (Cloudflare-gated
    # sites like MDPI/SAGE still challenge this — see README.)
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       f"Chrome/124.0 Safari/537.36 (mailto:{email})"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    # Wider connection pool so concurrent workers don't block on / warn about a
    # full pool (the default maxsize is 10).
    adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def get(session, url, *, params=None, timeout=40, stream=False):
    """GET with one retry on transient failure; returns Response or None.
    Honors 429/503 rate-limit signals (Retry-After) — important at 20k scale
    where Unpaywall/Europe PMC will throttle a fast run."""
    for attempt in (1, 2):
        try:
            r = session.get(url, params=params, timeout=timeout,
                            stream=stream, allow_redirects=True)
            if r.status_code == 200:
                return r
            # 404 from Unpaywall/EuropePMC just means "not there" — don't retry.
            if r.status_code in (400, 404):
                return r
            # Rate-limited / temporarily unavailable: wait the server-requested
            # time (capped) and retry once.
            if r.status_code in (429, 503) and attempt == 1:
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    wait = 5.0
                time.sleep(min(max(wait, 1.0), 30.0))
                continue
        except requests.RequestException:
            pass
        if attempt == 1:
            time.sleep(1.5)
    return None


# ---------- source 1: Europe PMC (clean JATS XML) ----------

def europepmc_lookup(session, doi: str) -> dict | None:
    """Look a DOI up in Europe PMC. Returns a small dict with the bits we need
    to fetch full text, or None if the DOI isn't indexed.

        {"pmcid": "PMC123" | None,   # -> NCBI / EPMC full-text XML
         "epmc_oa": bool,            # in EPMC's open full-text-XML subset
         "source": "MED"|"PMC"|...,  # EPMC archive code
         "id": "40879334"}
    """
    r = get(session, EUROPEPMC_SEARCH, params={
        "query": f'DOI:"{doi}"',
        "format": "json",
        "resultType": "core",
        "pageSize": 1,
    })
    if not r:
        return None
    try:
        results = r.json().get("resultList", {}).get("result", [])
    except ValueError:
        return None
    if not results:
        return None
    rec = results[0]
    return {
        "pmcid": rec.get("pmcid"),
        # EPMC only serves fullTextXML for its open-access subset.
        "epmc_oa": rec.get("isOpenAccess") == "Y" and rec.get("inEPMC") == "Y",
        "source": rec.get("source"),
        "id": rec.get("id"),
    }


def fetch_europepmc_xml(session, source: str, pid: str) -> tuple[str, str] | None:
    """Download + parse Europe PMC JATS full-text XML -> (text, url) or None."""
    url = EUROPEPMC_FULLTEXT.format(source=source, pid=pid)
    r = get(session, url)
    if not r or r.status_code != 200 or b"<" not in r.content[:200]:
        return None
    text = jats_to_text(r.content)
    return (text, url) if text else None


def fetch_ncbi_pmc(session, pmcid: str) -> tuple[str, str, str] | None:
    """Resolve a PMC article by id. Tries NCBI efetch JATS XML first (cleanest),
    then the PMC article HTML page. Returns (text, source_label, url) or None.
    """
    digits = pmcid.upper().replace("PMC", "")
    r = get(session, NCBI_EFETCH, params={
        "db": "pmc", "id": digits, "rettype": "full", "retmode": "xml",
    })
    if r and r.status_code == 200 and b"<body" in r.content:
        text = jats_to_text(r.content)
        if text:
            return (text, "ncbi_pmc_xml", r.url)
    # Fallback: the rendered PMC article page.
    html_url = NCBI_PMC_HTML.format(pmcid=pmcid.upper())
    text = fetch_html_text(session, html_url)
    if text:
        return (text, "ncbi_pmc_html", html_url)
    return None


def europepmc_pmcid_by_title(session, title: str, doi: str) -> str | None:
    """Conservative title search for a PMC copy when the DOI isn't indexed
    (e.g. MDPI/Frontiers papers Unpaywall 404s). Only accepts a hit whose DOI
    matches OR whose title matches near-exactly, to avoid grabbing the wrong
    paper."""
    title = (title or "").strip()
    if len(title) < 25:                  # too short to disambiguate safely
        return None
    r = get(session, EUROPEPMC_SEARCH, params={
        "query": f'TITLE:"{title}" AND (HAS_PMC:Y)',
        "format": "json", "resultType": "lite", "pageSize": 3,
    })
    if not r:
        return None
    try:
        results = r.json().get("resultList", {}).get("result", [])
    except ValueError:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for rec in results:
        if not rec.get("pmcid"):
            continue
        if doi and rec.get("doi", "").lower() == doi.lower():
            return rec["pmcid"]
        if norm(rec.get("title")) == norm(title):
            return rec["pmcid"]
    return None


# ---------- optional: headless-browser fallback (Cloudflare / JS sites) ----------

class BrowserRenderer:
    """One reusable headless Chromium for the whole run. Launching a browser
    per page (the naive approach) costs seconds each and makes a big run take
    hours; we start it ONCE, lazily, and reuse one context for every render.

    No-ops gracefully if Playwright isn't installed (install with:
    pip install playwright && python -m playwright install chromium).
    """
    def __init__(self):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._ok = True          # flips False if Playwright is unavailable

    def _ensure(self) -> bool:
        if self._ctx is not None:
            return True
        if not self._ok:
            return False
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._ctx = self._browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"))
            return True
        except Exception:
            self._ok = False     # don't retry on every paper
            return False

    def render(self, url: str, min_chars: int) -> tuple[str, str] | None:
        if not self._ensure():
            return None
        import trafilatura
        page = None
        try:
            page = self._ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)      # let Cloudflare/JS settle
            html = page.content()
        except Exception:
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
        text = trafilatura.extract(html, url=url, include_comments=False,
                                   include_tables=False, favor_recall=True)
        text = _clean_text(text) if text else ""
        return (text, url) if text and len(text) >= min_chars else None

    def close(self):
        for obj in (self._browser, self._pw):
            try:
                if obj is self._pw and obj is not None:
                    obj.stop()
                elif obj is not None:
                    obj.close()
            except Exception:
                pass


def jats_to_text(xml_bytes: bytes) -> str:
    """Flatten JATS/Europe-PMC full-text XML to readable plain text.

    Keeps the article title, abstract, and body section titles + paragraphs.
    Drops the reference list, tables, and figures (captions are noisy and the
    spec wants the paper's prose). Inline citation markers are stripped.
    """
    from lxml import etree
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return ""

    # Strip elements whose text we don't want in the body prose.
    for tag in ("ref-list", "table-wrap", "fig", "xref", "table",
                "disp-formula", "inline-formula"):
        for el in root.iter(tag):
            el.getparent().remove(el) if el.getparent() is not None else None

    def gather(el) -> str:
        return re.sub(r"[ \t]+", " ", "".join(el.itertext())).strip()

    parts: list[str] = []
    title = root.find(".//title-group/article-title")
    if title is not None:
        parts.append(gather(title))
    for ab in root.findall(".//abstract"):
        txt = gather(ab)
        if txt:
            parts.append("Abstract\n" + txt)
    body = root.find(".//body")
    if body is not None:
        for sec in body.iter("sec"):
            t = sec.find("title")
            if t is not None and gather(t):
                parts.append("\n" + gather(t))
            for p in sec.findall("p"):
                ptxt = gather(p)
                if ptxt:
                    parts.append(ptxt)
        if not body.findall(".//sec"):   # unsectioned body: just take paragraphs
            for p in body.findall(".//p"):
                ptxt = gather(p)
                if ptxt:
                    parts.append(ptxt)
    return _clean_text("\n\n".join(parts))


# ---------- source 0 (optional): Elsevier full-text API ----------

def is_elsevier(doi: str, publisher: str) -> bool:
    """Cheap check so we only spend an Elsevier API call on Elsevier DOIs."""
    if "elsevier" in (publisher or "").lower():
        return True
    # Common Elsevier DOI prefixes (10.1016 covers the vast majority).
    return doi.startswith(("10.1016/", "10.1006/", "10.1053/", "10.1067/",
                           "10.1078/", "10.3816/"))


def fetch_elsevier(session, doi: str, key: str) -> tuple[str, str] | None:
    """Fetch an Elsevier article's full text by DOI via the Article Retrieval
    API. Asks for text/plain (no parsing); falls back to flattening XML if the
    API hands back XML anyway. Returns (text, url) or None (e.g. not entitled)."""
    url = ELSEVIER_ARTICLE.format(doi=doi)
    try:
        r = session.get(url, headers={"X-ELS-APIKey": key,
                                      "Accept": "text/plain"}, timeout=60)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.content:
        return None
    body = r.content.lstrip()
    if body[:1] == b"<":                 # got XML — flatten it
        text = _xml_itertext(r.content)
    else:
        text = _clean_text(r.text)
    return (text, url) if text else None


def _xml_itertext(xml_bytes: bytes) -> str:
    """Last-resort flatten of arbitrary article XML (e.g. Elsevier's own schema,
    which isn't JATS). Drops obvious metadata/reference containers."""
    from lxml import etree
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return ""
    for tag in ("bibliography", "ref-list", "references", "coredata"):
        for el in root.iter("{*}" + tag):
            if el.getparent() is not None:
                el.getparent().remove(el)
    return _clean_text(" ".join(root.itertext()))


def is_wiley(doi: str, publisher: str) -> bool:
    if "wiley" in (publisher or "").lower():
        return True
    # Wiley/Blackwell DOI prefixes (10.1111 & 10.1002 dominate; 10.1155 = the
    # former Hindawi, now Wiley; 10.1113 = Physiological Society on Wiley).
    return doi.startswith(("10.1111/", "10.1002/", "10.1029/", "10.1046/",
                           "10.1034/", "10.1113/", "10.1155/", "10.1096/"))


def fetch_wiley(session, doi: str, token: str) -> tuple[str, str] | None:
    """Fetch a Wiley article PDF by DOI via the TDM API (institutional token),
    then extract text. Returns (text, url) or None (e.g. not entitled / not OA).
    """
    from urllib.parse import quote
    url = WILEY_TDM.format(doi=quote(doi, safe=""))
    try:
        r = session.get(url, headers={"Wiley-TDM-Client-Token": token},
                        stream=True, timeout=(10, 60), allow_redirects=True)
    except requests.RequestException:
        return None
    try:
        if r.status_code != 200:
            return None
        chunks, total = [], 0
        for chunk in r.iter_content(64 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                return None
        content = b"".join(chunks)
    except requests.RequestException:
        return None
    finally:
        r.close()
    text = fetch_pdf_text_from_bytes(content)
    return (text, url) if text else None


def is_springer(doi: str, publisher: str) -> bool:
    if "springer" in (publisher or "").lower():
        return True
    # Springer Nature DOI prefixes (10.1007 = Springer, 10.1186 = BMC,
    # 10.1038 = Nature, 10.1057 = Palgrave).
    return doi.startswith(("10.1007/", "10.1186/", "10.1038/", "10.1057/"))


def fetch_springer(session, doi: str, key: str) -> tuple[str, str] | None:
    """Fetch Springer Nature OA full text (JATS) by DOI. Returns (text, url) or
    None (e.g. not in the OA corpus)."""
    try:
        r = session.get(SPRINGER_OA, params={"q": f"doi:{doi}", "api_key": key},
                        headers={"Accept": "application/xml"}, timeout=60)
    except requests.RequestException:
        return None
    if r.status_code != 200 or b"<" not in r.content[:200]:
        return None
    text = jats_to_text(r.content)        # falls through cleanly if no <body>
    if not text or len(text) < 200:
        text = _xml_itertext(r.content)
    return (text, f"{SPRINGER_OA}?q=doi:{doi}") if text else None


# ---------- source 2/3: Unpaywall -> PDF or HTML ----------

def unpaywall_locations(session, doi: str, email: str) -> list[dict]:
    """Return Unpaywall OA locations for a DOI, best first ([] if none/closed)."""
    r = get(session, UNPAYWALL.format(doi=doi), params={"email": email})
    if not r or r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    locs = []
    best = data.get("best_oa_location")
    if best:
        locs.append(best)
    for loc in data.get("oa_locations", []) or []:
        if loc is not best:
            locs.append(loc)
    return locs


MAX_PDF_BYTES = 60 * 1024 * 1024     # don't let one giant/hanging PDF stall a run
# Some repositories (e.g. EconStor) serve the real PDF to a plain/bot UA but a
# JS interstitial to a browser UA — the reverse of most publisher CDNs. So if
# the browser UA doesn't yield a PDF, retry once with a bare UA.
PLAIN_UA = "python-requests/2 (mailto:oa-fulltext)"


def _download_pdf_bytes(session, url: str, ua: str | None) -> bytes | None:
    headers = {"User-Agent": ua} if ua else None
    try:
        r = session.get(url, stream=True, timeout=(10, 45),
                        allow_redirects=True, headers=headers)
    except requests.RequestException:
        return None
    try:
        if r.status_code != 200:
            return None
        chunks, total = [], 0
        for chunk in r.iter_content(64 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PDF_BYTES:        # bail on oversized downloads
                return None
        return b"".join(chunks)
    except requests.RequestException:
        return None
    finally:
        r.close()


def fetch_pdf_text(session, url: str) -> str | None:
    """Download a PDF (size- and time-capped) and extract text with PyMuPDF;
    None if it's not a real PDF or the download stalls/overruns. Retries with a
    bare UA when the default (browser) UA gets HTML instead of a PDF."""
    content = _download_pdf_bytes(session, url, ua=None)
    if not content or not content.startswith(b"%PDF"):
        content = _download_pdf_bytes(session, url, ua=PLAIN_UA)
    if not content or not content.startswith(b"%PDF"):
        return None
    import fitz  # PyMuPDF
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        pages = [doc.load_page(i).get_text("text") for i in range(doc.page_count)]
        doc.close()
    except Exception:
        return None
    return _clean_text("\n".join(pages))


CITATION_PDF_RE = re.compile(
    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
    re.I)
# rel="alternate" type="application/pdf" — another standard PDF advertisement.
ALT_PDF_RE = re.compile(
    r'<link[^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)["\']'
    r'|<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/pdf["\']',
    re.I)
# Repository (DSpace / EPrints / Pure) PDF links: /bitstream/.../x.pdf,
# /download, ?...format=pdf, or any href ending in .pdf.
REPO_PDF_RE = re.compile(
    r'href=["\']([^"\']*(?:/bitstream/[^"\']+|/download[^"\']*|[^"\']+\.pdf'
    r'(?:\?[^"\']*)?))["\']', re.I)


def find_pdf_links(html: str) -> list[str]:
    """Collect candidate full-text PDF URLs advertised on a page, best first:
    citation_pdf_url meta, then rel=alternate application/pdf, then repository
    bitstream/.pdf hrefs. Repository landing pages show only an abstract as HTML
    but link the real PDF this way — this is what rescues the thin-HTML hits."""
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str | None):
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    m = CITATION_PDF_RE.search(html)
    if m:
        add(m.group(1) or m.group(2))
    m = ALT_PDF_RE.search(html)
    if m:
        add(m.group(1) or m.group(2))
    for m in REPO_PDF_RE.finditer(html):
        add(m.group(1))
        if len(out) >= 6:        # cap so a link-heavy page doesn't explode
            break
    return out


def fetch_html_text(session, url: str) -> str | None:
    """Download an article page and return its text. Prefers a linked PDF
    (repository pages often expose only the abstract as HTML) but keeps the
    longer of {linked-PDF text, scraped HTML body}."""
    r = get(session, url, timeout=40)
    if not r:
        return None
    ctype = r.headers.get("Content-Type", "")
    if "pdf" in ctype.lower() or r.content[:4] == b"%PDF":
        return fetch_pdf_text_from_bytes(r.content)
    if "html" not in ctype.lower() and not r.text.lstrip().lower().startswith(("<!doc", "<html")):
        return None

    import trafilatura
    html_text = trafilatura.extract(
        r.text, url=url, include_comments=False, include_tables=False,
        favor_recall=True,
    )
    best = _clean_text(html_text) if html_text else ""

    # Try advertised PDF links; keep whichever gives the most text. Stop early
    # once a link clears the confident-full-text bar.
    from urllib.parse import urljoin
    for link in find_pdf_links(r.text):
        pdf_text = fetch_pdf_text(session, urljoin(r.url, link)) or ""
        if len(pdf_text) > len(best):
            best = pdf_text
        if len(best) >= CONFIDENT_CHARS:
            break
    return best or None


def fetch_pdf_text_from_bytes(content: bytes) -> str | None:
    if not content.startswith(b"%PDF"):
        return None
    import fitz
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception:
        return None
    pages = [doc.load_page(i).get_text("text") for i in range(doc.page_count)]
    doc.close()
    return _clean_text("\n".join(pages))


# ---------- text cleanup ----------

def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    # join words split across a line break by a hyphen ("hydro-\ndynamics")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# A "successful" fetch that is really a bot-challenge / block page (Anubis
# proof-of-work, Cloudflare "Just a moment", a plain Access Denied) clears the
# --min-chars bar with ~1k chars of boilerplate and gets saved as if it were
# the paper. Reject it so it counts as a miss, not a thin hit. We only inspect
# the head — these pages announce themselves in the first line or two, and this
# keeps a real paper that merely mentions e.g. "cloudflare" from being nuked.
BOTWALL_RE = re.compile(
    r"making sure you'?re not a bot|proof-of-work scheme|"
    r"just a moment\.\.\.|checking your browser before|"
    r"enable javascript and cookies to continue|"
    r"verify you are (?:a )?human|access to this page has been denied",
    re.I)


def _is_botwall(text: str) -> bool:
    return bool(text) and bool(BOTWALL_RE.search(text[:1500]))


# Auto-grade a retrieved paper so the manifest is self-certifying and the hit
# rate isn't inflated by non-papers. This is the audit_quality.py logic folded
# into the pipeline — every hit gets one of these labels:
#   clean        genuine full-text body
#   non_article  correction / erratum / editorial / meeting abstract — no body
#   refs_only    landing scrape that captured abstract + bibliography, no body
#   stub         short abstract / repository stub, body never captured
_NONARTICLE_RE = re.compile(
    r"^\s*(correction|corrigendum|erratum|retraction(?: notice)?|"
    r"editorial|withdrawal|author correction|publisher correction|"
    r"expression of concern|book review)\b", re.I)
_REFLINE_RE = re.compile(
    r"Google Scholar|CrossRef|PubMed|\[DOI\]|\[PMC|PMC free article|"
    r"doi\.org/10\.|Crossref Citations", re.I)
_SECTION_RE = re.compile(
    r"^\s*(introduction|background|method|materials|results|"
    r"discussion|conclusion)\b", re.I | re.M)


def classify_quality(text: str, source: str) -> str:
    n = len(text)
    head = text[:200].lstrip()
    if _NONARTICLE_RE.match(head):
        return "non_article"
    # body = prose before the reference list starts
    m = re.search(r"^\s*references\s*$", text, re.I | re.M)
    body_chars = len(text[:m.start()].strip()) if m else n
    has_body = bool(_SECTION_RE.search(text))
    lines = [ln for ln in text.splitlines() if ln.strip()] or [""]
    ref_frac = sum(1 for ln in lines if _REFLINE_RE.search(ln)) / len(lines)
    if body_chars < 3500 and not has_body:
        return "stub"
    if ref_frac > 0.5 and not has_body:
        return "refs_only"
    return "clean"


# ---------- orchestration ----------

def resolve_fulltext(session, doi: str, email: str, min_chars: int, *,
                     elsevier_key: str | None = None,
                     springer_key: str | None = None,
                     wiley_token: str | None = None,
                     publisher: str = "", title: str = "",
                     browser: "BrowserRenderer | None" = None,
                     deadline: float | None = None
                     ) -> tuple[str, str, str, str]:
    """Resolve a DOI to full text across every source, best-quality-first, and
    keep the LONGEST result ("best of locations") so a repository abstract never
    wins over a real full-text copy that also exists.

    Sources stop early the moment one clears CONFIDENT_CHARS (clearly full
    text); otherwise all are tried and the longest >= min_chars wins.

    Returns (text, source_label, url, reason). On a miss, text is "" and reason:
        not_indexed — no OA copy known anywhere (likely truly closed/non-article)
        oa_blocked  — an OA copy existed but every fetch was blocked / too short
    """
    saw_location = False
    candidates: list[tuple[str, str, str]] = []   # (text, source, url)

    def consider(got, label: str) -> bool:
        """Record a (text, url) result; return True if confident enough to stop.
        Bot-challenge pages are dropped here so they never count as text."""
        if got and got[0] and len(got[0]) >= min_chars and not _is_botwall(got[0]):
            candidates.append((got[0], label, got[1]))
            return len(got[0]) >= CONFIDENT_CHARS
        return False

    def best():
        text, label, url = max(candidates, key=lambda c: len(c[0]))
        return (text, label, url, "")

    def over_budget() -> bool:
        """True once we've spent the per-paper time budget. Checked before the
        slow fan-out steps so one hung DOI can't tie up a worker for minutes."""
        return deadline is not None and time.monotonic() > deadline

    # 0a. Elsevier full-text API (key + Elsevier DOI). Cleanest path past
    #     ScienceDirect's JS wall.
    if elsevier_key and is_elsevier(doi, publisher):
        saw_location = True
        if consider(fetch_elsevier(session, doi, elsevier_key), "elsevier_api"):
            return best()
    # 0b. Springer Nature OA API (key + Springer DOI) -> clean JATS.
    if springer_key and is_springer(doi, publisher):
        saw_location = True
        if consider(fetch_springer(session, doi, springer_key), "springer_api"):
            return best()
    # 0c. Wiley TDM API (token + Wiley DOI) -> entitled PDF past the wall.
    if wiley_token and is_wiley(doi, publisher):
        saw_location = True
        if consider(fetch_wiley(session, doi, wiley_token), "wiley_api"):
            return best()

    # 1. Europe PMC clean JATS XML (its OA subset).
    epmc = europepmc_lookup(session, doi)
    pmcid = epmc.get("pmcid") if epmc else None
    if epmc and epmc.get("epmc_oa") and epmc.get("source") and epmc.get("id"):
        saw_location = True
        if consider(fetch_europepmc_xml(session, epmc["source"], epmc["id"]),
                    "europepmc_xml"):
            return best()

    # 2. Unpaywall locations (also harvest a PMCID from any PMC URL).
    locs = unpaywall_locations(session, doi, email)
    if locs:
        saw_location = True
    for loc in locs:
        for u in (loc.get("url_for_pdf"), loc.get("url_for_landing_page"),
                  loc.get("url")):
            if u and not pmcid:
                m = PMCID_RE.search(u)
                if m:
                    pmcid = m.group(0)

    # 3. PMC route (NCBI efetch XML -> PMC HTML).
    if pmcid:
        saw_location = True
        got = fetch_ncbi_pmc(session, pmcid)
        if got and consider((got[0], got[2]), got[1]):
            return best()

    # 4. Unpaywall PDFs (every location).
    for loc in locs:
        if over_budget():
            break
        pdf_url = loc.get("url_for_pdf")
        if pdf_url and consider((fetch_pdf_text(session, pdf_url), pdf_url),
                                "unpaywall_pdf"):
            return best()

    # 5. Unpaywall landing pages (HTML body or a PDF the page links).
    for loc in locs:
        if over_budget():
            break
        page_url = loc.get("url_for_landing_page") or loc.get("url")
        if page_url and consider((fetch_html_text(session, page_url), page_url),
                                 "landing_html"):
            return best()

    # 6. Last-resort title search for a PMC copy — only if nothing solid yet
    #    (recovers MDPI/Frontiers whose repository deposit was abstract-only but
    #    that also sit in PMC). Strict title/DOI match guards against mismatches.
    confident = any(len(t) >= CONFIDENT_CHARS for t, _, _ in candidates)
    if not confident and not pmcid and title and not over_budget():
        tp = europepmc_pmcid_by_title(session, title, doi)
        if tp:
            saw_location = True
            got = fetch_ncbi_pmc(session, tp)
            if got:
                consider((got[0], got[2]), got[1])

    # 7. Headless-browser fallback for Cloudflare/JS publisher pages, last
    #    because it's slow. Tries the publisher copy via doi.org.
    if browser is not None and not any(len(t) >= CONFIDENT_CHARS
                                       for t, _, _ in candidates):
        got = browser.render(f"https://doi.org/{doi}", min_chars)
        if got:
            saw_location = True
            consider(got, "browser_html")

    if candidates:
        return best()
    return ("", "", "", "oa_blocked" if saw_location else "not_indexed")


def process_one(session, row: dict, out_dir: Path, email: str, min_chars: int,
                elsevier_key: str | None = None, springer_key: str | None = None,
                wiley_token: str | None = None,
                browser: "BrowserRenderer | None" = None,
                paper_timeout: float = 120.0) -> Result:
    pub_id = str(row[COL_ID]).strip()
    doi = str(row[COL_DOI]).strip()
    publisher = str(row.get(COL_PUBLISHER, "") or "")
    res = Result(pub_id=pub_id, doi=doi, publisher=publisher,
                 oa_type=str(row.get(COL_OA, "") or ""))
    if not doi or doi.lower() == "nan":
        res.status, res.note = "error", "missing DOI"
        return res
    deadline = time.monotonic() + paper_timeout if paper_timeout else None
    try:
        text, source, url, reason = resolve_fulltext(
            session, doi, email, min_chars,
            elsevier_key=elsevier_key, springer_key=springer_key,
            wiley_token=wiley_token, publisher=publisher,
            title=str(row.get(COL_TITLE, "") or ""), browser=browser,
            deadline=deadline)
    except Exception as e:                       # never let one DOI kill the run
        res.status, res.note = "error", f"{type(e).__name__}: {e}"
        return res
    if not text:
        res.status = reason or "no_oa_location"
        return res
    out_path = out_dir / f"{pub_id}.txt"
    out_path.write_text(text, encoding="utf-8")
    # Flag short scraped-HTML hits as likely abstract-only (see THIN_LANDING_CHARS).
    thin = source == "landing_html" and len(text) < THIN_LANDING_CHARS
    res.status = "ok_thin" if thin else "ok"
    res.source, res.url, res.chars = source, url, len(text)
    res.quality = classify_quality(text, source)
    if thin:
        res.note = "short HTML — likely abstract/repository stub, not full text"
    return res


# ---------- input selection ----------

def load_rows(input_path: Path) -> list[dict]:
    import pandas as pd
    df = pd.read_csv(input_path, dtype=str).fillna("")
    return df.to_dict("records")


def select_rows(rows: list[dict], *, ids: list[str] | None,
                sample: int | None, limit: int | None) -> list[dict]:
    """Pick which rows to fetch: explicit ids, or a per-publisher stratified
    sample (good for the practicability test), or just the first --limit."""
    if ids:
        wanted = set(ids)
        return [r for r in rows if str(r[COL_ID]).strip() in wanted]
    if sample:
        by_pub: dict[str, list[dict]] = {}
        for r in rows:
            by_pub.setdefault(str(r.get(COL_PUBLISHER, "")), []).append(r)
        picked: list[dict] = []
        for pub, group in sorted(by_pub.items()):
            # spread the sample across OA types within each publisher
            seen_oa: dict[str, int] = {}
            for r in group:
                oa = str(r.get(COL_OA, ""))
                if seen_oa.get(oa, 0) < max(1, sample // 2) and \
                        sum(seen_oa.values()) < sample:
                    picked.append(r)
                    seen_oa[oa] = seen_oa.get(oa, 0) + 1
            # top up to `sample` per publisher if OA spreading under-filled
            for r in group:
                if sum(1 for p in picked if p is r):
                    continue
                if sum(1 for p in picked
                       if str(p.get(COL_PUBLISHER, "")) == pub) >= sample:
                    break
                picked.append(r)
        return picked
    if limit:
        return rows[:limit]
    return rows


# ---------- manifest / resume ----------

def load_done(manifest_jsonl: Path) -> tuple[set[str], set[str]]:
    """Return (done_ok, done_miss): pub_ids already fetched OK, and pub_ids
    already recorded as a miss (oa_blocked / not_indexed). Resume always skips
    done_ok; done_miss is skipped too unless --retry-misses, so a big run
    doesn't re-hit thousands of dead DOIs (each several 45s timeouts) on every
    restart. `error` rows are NOT cached — those may be transient, so retry."""
    latest: dict[str, str] = {}          # pub_id -> most recent status
    if not manifest_jsonl.exists():
        return set(), set()
    with manifest_jsonl.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest[rec["pub_id"]] = str(rec.get("status", ""))
    done_ok = {p for p, s in latest.items() if s.startswith("ok")}
    done_miss = {p for p, s in latest.items()
                 if s in ("oa_blocked", "not_indexed")}
    return done_ok, done_miss


def write_manifest_csv(records: list[Result], path: Path) -> None:
    fields = list(asdict(records[0]).keys()) if records else \
        [f.name for f in Result.__dataclass_fields__.values()]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow(asdict(r))


def run(input_path: Path, out_dir: Path, email: str, *,
        ids: list[str] | None, sample: int | None, limit: int | None,
        min_chars: int, delay: float, resume: bool, workers: int = 8,
        retry_misses: bool = False, recheck_misses: bool = False,
        miss_retries: int = 0, paper_timeout: float = 120.0,
        elsevier_key: str | None = None, springer_key: str | None = None,
        wiley_token: str | None = None, use_browser: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_jsonl = out_dir / "_manifest.jsonl"
    manifest_csv = out_dir / "_manifest.csv"

    rows = load_rows(input_path)
    # --recheck-misses re-attempts every DOI currently recorded as a miss (PMC
    # catches up over time, VPN now on, a key was added...), ignoring the normal
    # --sample/--limit selection. Needs the existing manifest, so it implies resume.
    if recheck_misses:
        resume = retry_misses = True
    todo = select_rows(rows, ids=ids, sample=sample, limit=limit)
    done_ok, done_miss = load_done(manifest_jsonl) if resume else (set(), set())
    if not resume:
        manifest_jsonl.unlink(missing_ok=True)
    prior_miss = set(done_miss)
    if recheck_misses:
        todo = [r for r in rows if str(r[COL_ID]).strip() in done_miss]

    session = make_session(email)
    browser = BrowserRenderer() if use_browser else None
    # Playwright's sync API is single-thread-only, so the browser fallback forces
    # serial mode. Otherwise fetch `workers` papers at once — the per-paper design
    # (each writes its own .txt + one manifest line) makes this safe with just a
    # lock around the shared manifest append and the progress counter.
    n_workers = 1 if use_browser else max(1, workers)

    print(f"Input: {input_path.name}  ({len(rows)} rows)")
    print(f"Selected {len(todo)} publications to fetch "
          f"({'resume, ' + str(len(done_ok)) + ' done + ' + str(len(done_miss)) + ' cached misses' if resume else 'fresh run'})")
    print(f"Output dir: {out_dir}")
    print(f"Keys: Elsevier {'on' if elsevier_key else 'off'}, "
          f"Springer {'on' if springer_key else 'off'}, "
          f"Wiley {'on' if wiley_token else 'off'}  |  "
          f"browser fallback: {'on' if use_browser else 'off'}  |  "
          f"workers: {n_workers}  |  paper timeout: {paper_timeout:g}s\n")

    # Build the work list. Resume skips already-fetched OK ids, and (unless
    # --retry-misses) already-recorded misses too, so a restart doesn't re-hit
    # thousands of dead DOIs.
    pending: list[dict] = []
    n_skip_ok = n_skip_miss = 0
    for row in todo:
        pub_id = str(row[COL_ID]).strip()
        if resume and pub_id in done_ok:
            n_skip_ok += 1
        elif resume and pub_id in done_miss and not retry_misses:
            n_skip_miss += 1
        else:
            pending.append(row)
    if resume and (n_skip_ok or n_skip_miss):
        print(f"  skipping {n_skip_ok} already-done + {n_skip_miss} cached misses"
              f"{' (--retry-misses to re-attempt misses)' if n_skip_miss else ''}")
    records: list[Result] = []
    lock = threading.Lock()

    def fetch_batch(batch: list[dict], label: str = "") -> None:
        """Fetch a list of rows concurrently, appending each result to the
        manifest under the lock. Reused for the main pass and each retry round."""
        total = len(batch)
        counter = {"n": 0}

        def work(row: dict) -> Result:
            res = process_one(session, row, out_dir, email, min_chars,
                              elsevier_key, springer_key, wiley_token, browser,
                              paper_timeout)
            with lock:                   # serialize manifest append + progress
                counter["n"] += 1
                k = counter["n"]
                with manifest_jsonl.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(res), ensure_ascii=False) + "\n")
                records.append(res)
                flag = "OK " if res.status == "ok" else "-- "
                print(f"[{label}{k}/{total}] {res.pub_id}  ({res.publisher})  {res.doi}")
                print(f"        {flag}{res.status:16} {res.source:14} "
                      f"{res.chars:>7} chars  {res.note}")
            return res

        if n_workers == 1:
            for row in batch:
                work(row)
                time.sleep(delay)
        else:
            # NB: don't use `with ThreadPoolExecutor` — its __exit__ waits for
            # EVERY queued task, so Ctrl+C wouldn't cancel until the whole batch
            # finished. Instead, on interrupt we cancel the not-yet-started tasks
            # and let only the in-flight ones drain (bounded by --paper-timeout).
            ex = ThreadPoolExecutor(max_workers=n_workers)
            futures = [ex.submit(work, row) for row in batch]
            try:
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:      # a worker shouldn't crash, but
                        with lock:              # never let one kill the pool
                            print(f"        !! worker error: {type(e).__name__}: {e}")
            except KeyboardInterrupt:
                with lock:
                    print("\n^C — cancelling queued papers; letting in-flight "
                          "finish (a few, bounded by --paper-timeout). "
                          "Re-run the same command to resume.")
                for f in futures:
                    f.cancel()              # cancels only not-yet-started tasks
                ex.shutdown(wait=False)
                raise
            ex.shutdown(wait=False)

    interrupted = False
    try:
        fetch_batch(pending)
        # Auto-retry misses within the same run: many are transient (rate-limit,
        # timeout, network), and a retry over the smaller miss set — less load —
        # clears them. Stop as soon as a round recovers nothing (converged), or
        # after --miss-retries rounds.
        for rnd in range(1, miss_retries + 1):
            _, miss_now = load_done(manifest_jsonl)
            retry_rows = [r for r in rows if str(r[COL_ID]).strip() in miss_now]
            if not retry_rows:
                break
            before = len(miss_now)
            print(f"\n-- retry round {rnd}/{miss_retries}: re-attempting "
                  f"{len(retry_rows)} misses --")
            fetch_batch(retry_rows, label=f"r{rnd}:")
            _, miss_after = load_done(manifest_jsonl)
            recovered = before - len(miss_after)
            print(f"-- round {rnd}: recovered {recovered} --")
            if recovered <= 0:
                break
    except KeyboardInterrupt:
        interrupted = True       # progress is already on disk; save + report below
    finally:
        if browser is not None:
            browser.close()

    # rebuild the CSV manifest from the full jsonl (incl. prior resumed rows)
    all_records = _read_all_records(manifest_jsonl)
    if all_records:
        write_manifest_csv(all_records, manifest_csv)
    _print_summary(all_records or records)
    if recheck_misses and prior_miss:
        recovered = sum(1 for r in records if r.status.startswith("ok")
                        and r.pub_id in prior_miss)
        print(f"\nRecheck: recovered {recovered}/{len(prior_miss)} "
              f"previously-missed papers this pass.")
    if interrupted:
        print("\n** Interrupted — progress saved. Re-run the same command to "
              "resume where it stopped. **")
    print(f"\nManifest: {manifest_csv}")


def _read_all_records(manifest_jsonl: Path) -> list[Result]:
    """Read the append-only jsonl, keeping the LAST record per pub_id. Re-fetched
    papers (e.g. --recheck-misses that flips a miss to ok) append a new line; this
    dedup makes the newest result win so the CSV/summary never double-count."""
    latest: dict[str, Result] = {}
    order: list[str] = []
    if not manifest_jsonl.exists():
        return []
    with manifest_jsonl.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = Result(**json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
            if r.pub_id not in latest:
                order.append(r.pub_id)
            latest[r.pub_id] = r
    return [latest[p] for p in order]


def _print_summary(records: list[Result]) -> None:
    if not records:
        print("\nNo records.")
        return
    n = len(records)
    full = [r for r in records if r.status == "ok"]
    thin = [r for r in records if r.status == "ok_thin"]
    got = full + thin
    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(got)}/{n} retrieved "
          f"({100*len(got)//n if n else 0}%)  "
          f"[{len(full)} full text + {len(thin)} thin/abstract-only]")
    # quality breakdown of hits — the honest full-text count
    q: dict[str, int] = {}
    for r in got:
        q[r.quality or "clean"] = q.get(r.quality or "clean", 0) + 1
    clean = q.get("clean", 0)
    extra = ", ".join(f"{c} {k}" for k, c in sorted(q.items())
                      if k != "clean" and c)
    print(f"  quality: {clean} clean full text"
          + (f"  ({extra})" if extra else "")
          + f"  ->  {100*clean//n if n else 0}% genuine full text")
    # by source
    by_source: dict[str, int] = {}
    for r in got:
        by_source[r.source] = by_source.get(r.source, 0) + 1
    for src, c in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"  source {src:14} {c}")
    # by publisher: full / total (thin shown separately)
    pubs: dict[str, list[int]] = {}
    for r in records:
        pubs.setdefault(r.publisher, [0, 0, 0])
        pubs[r.publisher][2] += 1
        if r.status == "ok":
            pubs[r.publisher][0] += 1
        elif r.status == "ok_thin":
            pubs[r.publisher][1] += 1
    print("  ---- by publisher (full text / total; +thin) ----")
    for pub, (hit, th, tot) in sorted(pubs.items()):
        extra = f"  (+{th} thin)" if th else ""
        print(f"  {pub:34} {hit}/{tot}{extra}")
    # failure reasons
    fails: dict[str, int] = {}
    for r in records:
        if not r.status.startswith("ok"):
            fails[r.status] = fails.get(r.status, 0) + 1
    if fails:
        print("  ---- misses ----")
        for st, c in sorted(fails.items(), key=lambda x: -x[1]):
            print(f"  {st:18} {c}")
    print("=" * 60)


def load_env_file(path: Path) -> None:
    """Minimal .env reader: set KEY=VALUE lines into os.environ (without
    overriding anything already set). Avoids a python-dotenv dependency.
    Lines starting with # and blank lines are ignored; surrounding quotes on
    the value are stripped."""
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="CSV with 'Publication ID' + 'DOI' columns")
    ap.add_argument("--out", default="out", help="Output directory")
    ap.add_argument("--email", default=DEFAULT_EMAIL,
                    help="Contact email for Unpaywall / Europe PMC polite pool")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--ids", nargs="*", help="Specific Publication IDs to fetch")
    grp.add_argument("--sample", type=int,
                     help="Fetch ~N papers PER publisher, spread across OA "
                          "types (for the practicability test)")
    grp.add_argument("--limit", type=int, help="Fetch only the first N rows")
    ap.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS,
                    help="Min chars to count an extraction as success "
                         f"(default {DEFAULT_MIN_CHARS})")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds to sleep between papers in serial mode "
                         "(--workers 1 / --use-browser). Ignored when workers>1.")
    ap.add_argument("--workers", type=int, default=8,
                    help="Papers to fetch concurrently (default 8). Forced to 1 "
                         "with --use-browser (Playwright is single-threaded).")
    ap.add_argument("--restart", action="store_true",
                    help="Ignore any existing manifest and start fresh "
                         "(default is to resume / skip done ids)")
    ap.add_argument("--retry-misses", action="store_true",
                    help="On resume, also re-attempt papers previously recorded "
                         "as oa_blocked / not_indexed (default: skip them, so a "
                         "big run doesn't re-hit dead DOIs every restart).")
    ap.add_argument("--recheck-misses", action="store_true",
                    help="Re-attempt ONLY the DOIs currently recorded as misses "
                         "(ignores --sample/--limit). Run this weeks later to "
                         "catch papers PMC has since indexed; reports how many "
                         "were recovered.")
    ap.add_argument("--miss-retries", type=int, default=0,
                    help="After the main pass, auto-retry the misses up to N "
                         "rounds within the same run (stops early once a round "
                         "recovers nothing). Clears transient rate-limit/timeout "
                         "failures — folds the manual recheck loop into one run.")
    ap.add_argument("--paper-timeout", type=float, default=120.0,
                    help="Soft per-paper time budget in seconds (default 120). "
                         "Stops trying more sources once exceeded so one hung "
                         "DOI can't stall a worker. 0 disables.")
    ap.add_argument("--elsevier-key", default=None,
                    help="Elsevier Article Retrieval API key (or set "
                         "$ELSEVIER_API_KEY). Clean full-text for "
                         "Elsevier/ScienceDirect DOIs, the biggest miss bucket.")
    ap.add_argument("--springer-key", default=None,
                    help="Springer Nature OA API key (or set $SPRINGER_API_KEY). "
                         "Clean JATS full-text for Springer/BMC/Nature OA DOIs.")
    ap.add_argument("--wiley-token", default=None,
                    help="Wiley TDM client token (or set $WILEY_TDM_TOKEN). "
                         "Institutional token; returns entitled Wiley PDFs by DOI.")
    ap.add_argument("--use-browser", action="store_true",
                    help="Last-resort headless-browser (Playwright) fallback for "
                         "Cloudflare/JS publisher pages (MDPI/SAGE/Elsevier). "
                         "Slow; needs 'pip install playwright && python -m "
                         "playwright install chromium'.")
    ap.add_argument("--col-id", default=None)
    ap.add_argument("--col-doi", default=None)
    args = ap.parse_args()

    import os
    # Load keys from a .env file next to this script (if present) so you don't
    # have to export them every session. Real flags / existing env vars win.
    load_env_file(Path(__file__).resolve().parent / ".env")
    elsevier_key = args.elsevier_key or os.environ.get("ELSEVIER_API_KEY")
    springer_key = args.springer_key or os.environ.get("SPRINGER_API_KEY")
    wiley_token = args.wiley_token or os.environ.get("WILEY_TDM_TOKEN")

    # allow header overrides for a differently-shaped input
    global COL_ID, COL_DOI
    if args.col_id:
        COL_ID = args.col_id
    if args.col_doi:
        COL_DOI = args.col_doi

    run(Path(args.input), Path(args.out), args.email,
        ids=args.ids, sample=args.sample, limit=args.limit,
        min_chars=args.min_chars, delay=args.delay, resume=not args.restart,
        workers=args.workers, retry_misses=args.retry_misses,
        recheck_misses=args.recheck_misses, miss_retries=args.miss_retries,
        paper_timeout=args.paper_timeout,
        elsevier_key=elsevier_key, springer_key=springer_key,
        wiley_token=wiley_token, use_browser=args.use_browser)


if __name__ == "__main__":
    main()

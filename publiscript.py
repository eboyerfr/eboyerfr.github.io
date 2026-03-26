#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
import time
import html
from pathlib import Path
from urllib.parse import quote_plus

import requests
import feedparser


# -----------------------------
# Configuration
# -----------------------------

AUTHOR_ID = "1719388"
OUTPUT_JSON = "publications.json"
EXTRAS_JSON = "pubs-extra.json"
CACHE_DIR = Path(".cache")
S2_CACHE_FILE = CACHE_DIR / "semantic_scholar_author_1719388.json"

HEADERS = {
    "User-Agent": "publiscript/1.0"
}


# -----------------------------
# Helpers
# -----------------------------

def slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_title(text: str) -> str:
    text = html.unescape(text or "")
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return text


def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


def safe_get(dct, *keys, default=""):
    cur = dct
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def request_json(url: str, params=None, timeout=30, max_retries=6):
    delay = 2.0

    for attempt in range(max_retries):
        r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)

        if r.status_code == 429:
            print(f"[429] Too Many Requests -> sleep {delay:.1f}s")
            time.sleep(delay)
            delay *= 2
            continue

        if not r.ok:
            print("HTTP error")
            print("URL   :", r.url)
            print("Code  :", r.status_code)
            print("Body  :", r.text[:2000])
            r.raise_for_status()

        return r.json()

    raise RuntimeError(f"Too many retries for {url}")


def request_text(url: str, params=None, timeout=30, max_retries=5):
    delay = 2.0

    for attempt in range(max_retries):
        r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)

        if r.status_code == 429:
            print(f"[429] Too Many Requests -> sleep {delay:.1f}s")
            time.sleep(delay)
            delay *= 2
            continue

        if not r.ok:
            print("HTTP error")
            print("URL   :", r.url)
            print("Code  :", r.status_code)
            print("Body  :", r.text[:2000])
            r.raise_for_status()

        return r.text

    raise RuntimeError(f"Too many retries for {url}")


# -----------------------------
# Semantic Scholar
# -----------------------------

def get_author_papers(author_id: str):
    fields = ",".join([
        "name",
        "paperCount",
        "hIndex",
        "url",
        "papers.title",
        "papers.year",
        "papers.venue",
        "papers.url",
        "papers.externalIds",
        "papers.authors",
        "papers.openAccessPdf",
        "papers.journal",
        "papers.citationCount",
        "papers.influentialCitationCount",
    ])

    url = f"https://api.semanticscholar.org/graph/v1/author/{author_id}"
    params = {"fields": fields}
    data = request_json(url, params=params)
    return data


def get_author_papers_cached(author_id: str, force_refresh=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if S2_CACHE_FILE.exists() and not force_refresh:
        print(f"Loading Semantic Scholar cache: {S2_CACHE_FILE}")
        with S2_CACHE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)

    print("Fetching Semantic Scholar data...")
    data = get_author_papers(author_id)

    with S2_CACHE_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return data


# -----------------------------
# Crossref
# -----------------------------

def extract_crossref_year(item):
    for key in ("published-print", "published-online", "issued"):
        parts = safe_get(item, key, "date-parts", default=[])
        if parts and parts[0]:
            return parts[0][0]
    return None


def extract_crossref_authors(item):
    out = []
    for a in item.get("author", []) or []:
        given = a.get("given", "").strip()
        family = a.get("family", "").strip()
        name = (given + " " + family).strip()
        if name:
            out.append(name)
    return ", ".join(out)


def extract_crossref_venue(item):
    for key in ("container-title", "short-container-title"):
        vals = item.get(key) or []
        if vals:
            return vals[0]
    return ""


def extract_best_pdf_from_crossref(item):
    for link in item.get("link", []) or []:
        ctype = (link.get("content-type") or "").lower()
        if "pdf" in ctype:
            return link.get("URL", "")
    return ""


def crossref_lookup(title: str, year=None):
    url = "https://api.crossref.org/works"
    query = title if not year else f"{title} {year}"
    params = {
        "query.bibliographic": query,
        "rows": 5,
        "select": "DOI,title,author,container-title,short-container-title,published-print,published-online,issued,URL,link,type"
    }

    data = request_json(url, params=params)
    items = safe_get(data, "message", "items", default=[])

    if not items:
        return None

    nt = normalize_title(title)
    scored = []

    for item in items:
        t = ""
        if item.get("title"):
            t = item["title"][0]

        score = 0
        nt_item = normalize_title(t)

        if nt_item == nt:
            score += 100
        elif nt and nt_item.startswith(nt[:40]):
            score += 40

        item_year = extract_crossref_year(item)
        if year and item_year and str(item_year) == str(year):
            score += 10

        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else None


# -----------------------------
# arXiv fallback
# -----------------------------

def arxiv_lookup(title: str):
    query = f'ti:"{title}"'
    url = f"http://export.arxiv.org/api/query?search_query={quote_plus(query)}&start=0&max_results=3"

    try:
        txt = request_text(url, timeout=30)
    except Exception:
        return None

    feed = feedparser.parse(txt)
    if not feed.entries:
        return None

    nt = normalize_title(title)
    best = None
    best_score = -1

    for e in feed.entries:
        t = e.get("title", "")
        score = 0
        nt_item = normalize_title(t)

        if nt_item == nt:
            score += 100
        elif nt and nt_item.startswith(nt[:40]):
            score += 40

        if score > best_score:
            best_score = score
            best = e

    return best


# -----------------------------
# Website-specific helpers
# -----------------------------

def guess_teaser_path(title: str):
    slug = slugify(title)
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = Path("images") / f"{slug}.{ext}"
        if p.exists():
            return str(p).replace("\\", "/")
    return ""


def stable_id_from_title(title: str):
    slug = slugify(title)
    return slug if slug else f"pub-{abs(hash(title))}"


def merge_extras(item, extras):
    key = item["id"]
    if key in extras and isinstance(extras[key], dict):
        merged = dict(item)
        merged.update(extras[key])
        return merged
    return item


def deduplicate_publications(items):
    seen = {}

    for it in items:
        key = normalize_title(it.get("title", "")) or it["id"]

        if key not in seen:
            seen[key] = it
            continue

        old = seen[key]

        richness_old = sum(bool(old.get(k)) for k in [
            "authors", "venue", "year", "doi", "paper", "video", "teaser", "project", "code"
        ])
        richness_new = sum(bool(it.get(k)) for k in [
            "authors", "venue", "year", "doi", "paper", "video", "teaser", "project", "code"
        ])

        if richness_new > richness_old:
            seen[key] = it

    return list(seen.values())


# -----------------------------
# Main
# -----------------------------

def main(force_refresh=False):
    extras = {}
    extras_path = Path(EXTRAS_JSON)

    if extras_path.exists():
        with extras_path.open("r", encoding="utf-8") as f:
            extras = json.load(f)

    print(f"Using Semantic Scholar author id: {AUTHOR_ID}")

    data = get_author_papers_cached(AUTHOR_ID, force_refresh=force_refresh)
    papers = data.get("papers", [])

    print(f"Fetched {len(papers)} papers")

    publications = []

    for i, p in enumerate(papers, 1):
        title = p.get("title", "") or ""
        if not title:
            continue

        year = p.get("year", "") or ""
        s2_authors = ", ".join(
            a.get("name", "") for a in p.get("authors", []) if a.get("name")
        )
        s2_venue = p.get("venue", "") or safe_get(p, "journal", "name", default="")
        ext_ids = p.get("externalIds", {}) or {}

        doi = ext_ids.get("DOI", "") or ""
        arxiv_id = ext_ids.get("ArXiv", "") or ""
        corpus_id = ext_ids.get("CorpusId", "") or ""

        semantic_url = p.get("url", "") or ""
        open_pdf = safe_get(p, "openAccessPdf", "url", default="")
        paper_url = open_pdf or semantic_url
        doi_url = f"https://doi.org/{doi}" if doi else ""

        cross = None
        try:
            time.sleep(0.4)
            cross = crossref_lookup(title, year=year)
        except Exception as e:
            print(f"[warn] Crossref failed for '{title[:80]}': {e}")

        arx = None
        if not arxiv_id and not paper_url:
            try:
                time.sleep(0.4)
                arx = arxiv_lookup(title)
            except Exception as e:
                print(f"[warn] arXiv failed for '{title[:80]}': {e}")

        authors = s2_authors
        venue = s2_venue
        pdf_url = open_pdf

        if cross:
            cross_authors = extract_crossref_authors(cross)
            cross_venue = extract_crossref_venue(cross)
            cross_doi = cross.get("DOI", "") or ""
            cross_pdf = extract_best_pdf_from_crossref(cross)

            if not authors:
                authors = cross_authors
            if not venue:
                venue = cross_venue
            if not doi:
                doi = cross_doi
                doi_url = f"https://doi.org/{doi}" if doi else ""
            if not pdf_url:
                pdf_url = cross_pdf

        if arx and not pdf_url:
            for link in arx.get("links", []):
                href = link.get("href", "")
                if href.endswith(".pdf") or "/pdf/" in href:
                    pdf_url = href
                    break

            if not pdf_url:
                pdf_url = arx.get("id", "")

        final_paper = pdf_url or doi_url or paper_url

        item = {
            "id": stable_id_from_title(title),
            "title": title,
            "authors": authors,
            "venue": venue,
            "year": year,
            "paper": final_paper,
            "project": "",
            "code": "",
            "video": "",
            "teaser": guess_teaser_path(title),
            "doi": doi,
            "doi_url": doi_url,
            "semantic_scholar_url": semantic_url,
            "citationCount": p.get("citationCount", 0) or 0,
            "influentialCitationCount": p.get("influentialCitationCount", 0) or 0,
            "externalIds": ext_ids,
            "corpusId": corpus_id,
        }

        item = merge_extras(item, extras)
        publications.append(item)

        print(f"[{i:03d}/{len(papers):03d}] {title}")

    publications = deduplicate_publications(publications)

    publications.sort(
        key=lambda x: (
            safe_int(x.get("year", 0), 0),
            safe_int(x.get("citationCount", 0), 0)
        ),
        reverse=True
    )

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(publications, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {len(publications)} publications to {OUTPUT_JSON}")

    missing_authors = sum(1 for p in publications if not p.get("authors"))
    missing_venue = sum(1 for p in publications if not p.get("venue"))
    missing_video = sum(1 for p in publications if not p.get("video"))
    missing_teaser = sum(1 for p in publications if not p.get("teaser"))

    print(f"Missing authors: {missing_authors}")
    print(f"Missing venue:   {missing_venue}")
    print(f"Missing video:   {missing_video}")
    print(f"Missing teaser:  {missing_teaser}")


if __name__ == "__main__":
    main(force_refresh=False)
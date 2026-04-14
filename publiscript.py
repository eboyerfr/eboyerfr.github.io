import json
import re
from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin

import fitz
import requests
from bs4 import BeautifulSoup

URL = "https://morpheo.inrialpes.fr/people/Boyer/index.php?id=elements"
OUTPUT = Path("publications.json")
MANUAL_FILE = Path("pubmanual.json")
GITHUB_CACHE_FILE = Path("github_cache.json")
THUMB_DIR = Path("assets/publications")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------
# Base utils
# ---------------------------------------------------------------------
def clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def get_year(text: str):
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", text or "")
    return int(years[-1]) if years else None


def shorten_authors(authors: str, max_authors: int = 5) -> str:
    parts = [a.strip() for a in authors.split(",") if a.strip()]
    if len(parts) <= max_authors:
        return ", ".join(parts)
    return ", ".join(parts[:max_authors]) + ", et al."


def clean_venue(raw: str) -> str:
    s = clean(raw)
    s = re.sub(r"\?\s*10\.\S+\s*\?", "", s)
    s = re.sub(r"https?://dx\.doi\.org/\S+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bdoi\s*:\s*\S+", "", s, flags=re.IGNORECASE)
    s = re.sub(r",?\s*pp\.\s*\d+\s*-\s*\d+\.?", "", s, flags=re.IGNORECASE)
    s = re.sub(r",?\s*pp\.\s*\d+\.?", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ", ", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+\.", ".", s)
    return s.strip(" ,;")


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:100] if text else "paper"


def normalize_title(title: str) -> str:
    s = (title or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_tokens(title: str):
    stop = {
        "a", "an", "the", "for", "of", "on", "and", "with", "from", "to",
        "in", "by", "via", "using", "towards", "toward", "into", "based",
        "single", "image", "images", "model", "models", "neural", "implicit"
    }
    return [t for t in normalize_text(title).split() if len(t) > 2 and t not in stop]


def load_json_file(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json_file(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------
# Extraction of fields
# ---------------------------------------------------------------------
def extract_title_and_url(dl, base_url: str):
    node = dl.select_one("dd.Titre a, .Titre a")
    if not node:
        return "", ""
    title = clean(node.get_text())
    url = urljoin(base_url, node.get("href", ""))
    return title, url


def extract_authors(dl):
    node = dl.select_one("dd.Auteurs, .Auteurs")
    if not node:
        return "", ""
    authors_full = clean(node.get_text())
    authors_short = shorten_authors(authors_full)
    return authors_short, authors_full


def extract_venue(dl):
    node = dl.select_one("dd.article, .article")
    if not node:
        return ""
    return clean_venue(node.get_text(" ", strip=True))


def classify_url(url: str) -> str:
    low = (url or "").strip().lower()

    if not low:
        return ""

    if "github.com" in low or "gitlab.com" in low or "bitbucket.org" in low:
        return "code"

    if low.endswith("/bibtex") or "bibtex" in low:
        return "bibtex_url"

    if ".pdf" in low:
        return "pdf"

    if (
        ".mp4" in low
        or ".webm" in low
        or ".mov" in low
        or "youtube.com" in low
        or "youtu.be" in low
        or "vimeo.com" in low
    ):
        return "video"

    return ""


def extract_links(dl, base_url: str):
    pdf = ""
    video = ""
    code = ""
    bibtex_url = ""

    def register_url(raw_url: str):
        nonlocal pdf, video, code, bibtex_url

        href = urljoin(base_url, raw_url).strip()
        kind = classify_url(href)

        if kind == "pdf" and not pdf:
            pdf = href
        elif kind == "video" and not video:
            video = href
        elif kind == "code" and not code:
            code = href
        elif kind == "bibtex_url" and not bibtex_url:
            bibtex_url = href

    # 1) all explicit links
    for a in dl.find_all("a", href=True):
        register_url(a["href"])

    # 2) raw URLs embedded in text blocks
    for node in dl.select("dd.video, .video, dd.Url, .Url, dd.Notes, .Notes"):
        txt = clean(node.get_text(" ", strip=True))
        urls = re.findall(r"https?://[^\s<>\"]+", txt)
        for u in urls:
            register_url(u)

    return pdf, video, bibtex_url, code


# ---------------------------------------------------------------------
# GitHub / GitLab / Bitbucket enrichment
# ---------------------------------------------------------------------
def looks_like_valid_github_url(url: str) -> bool:
    if not url:
        return False

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    bad_patterns = [
        "/issues", "/pull", "/pulls", "/actions", "/commits", "/commit/",
        "/releases", "/release/", "/packages", "/projects", "/discussions",
        "/stargazers", "/watchers", "/network", "/search", "/wiki", "/users/"
    ]
    if any(p in path for p in bad_patterns):
        return False

    if host.endswith(".github.io"):
        return True

    if host == "github.com":
        parts = [p for p in path.split("/") if p]
        return len(parts) >= 2

    return False


def score_github_candidate(url: str, title: str) -> int:
    score = 0
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = unquote(parsed.path.lower())

    toks = title_tokens(title)
    overlap = sum(1 for t in toks if t in path)

    if host.endswith(".github.io"):
        score += 30
    elif host == "github.com":
        score += 20

    score += min(overlap, 6) * 5

    if toks and toks[0] in path:
        score += 10

    return score


def extract_result_links_from_duckduckgo(html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a in soup.select("a.result__a"):
        href = a.get("href", "").strip()
        if href:
            links.append(href)

    return links


def search_github_from_title(title: str, session: requests.Session, github_cache: dict) -> str:
    key = normalize_title(title)
    if key in github_cache:
        return github_cache[key]

    queries = [
        f'"{title}" github',
        f'"{title}" site:github.com',
        f'"{title}" site:github.io',
    ]

    candidates = []

    for q in queries:
        try:
            r = session.get(
                "https://html.duckduckgo.com/html/",
                params={"q": q},
                headers=HEADERS,
                timeout=20,
            )
            r.raise_for_status()

            for href in extract_result_links_from_duckduckgo(r.text):
                if looks_like_valid_github_url(href):
                    candidates.append(href)

        except Exception:
            continue

    candidates = list(dict.fromkeys(candidates))

    if not candidates:
        github_cache[key] = ""
        return ""

    ranked = sorted(
        candidates,
        key=lambda u: score_github_candidate(u, title),
        reverse=True
    )

    best = ranked[0]
    best_score = score_github_candidate(best, title)

    result = best if best_score >= 20 else ""
    github_cache[key] = result
    return result


def enrich_publications_with_github(publications, session, github_cache):
    for idx, pub in enumerate(publications, start=1):
        ensure_publication_fields(pub)

        if not pub.get("code") and pub.get("title"):
            pub["code"] = search_github_from_title(pub["title"], session, github_cache)

        if idx % 25 == 0:
            print(f"GitHub enrichissement : {idx}/{len(publications)}")


# ---------------------------------------------------------------------
# Publication object normalization
# ---------------------------------------------------------------------
def ensure_publication_fields(pub: dict):
    defaults = {
        "title": "",
        "authors": "",
        "authors_full": "",
        "venue": "",
        "year": None,
        "pdf": "",
        "video": "",
        "thumbnail": "",
        "scholar_url": "",
        "code": "",
        "url": "",
        "bibtex_url": "",
    }
    for k, v in defaults.items():
        if k not in pub:
            pub[k] = v

    # compatibility with old key
    #if not pub.get("code") and pub.get("github"):
     #   pub["code"] = pub["github"]

    # safety: if a previous json accidentally put a code repo into video
    v = (pub.get("video") or "").lower()
    if ("github.com" in v or "gitlab.com" in v or "bitbucket.org" in v):
        if not pub.get("code"):
            pub["code"] = pub["video"]
        pub["video"] = ""

    return pub


# ---------------------------------------------------------------------
# Thumbnails
# - local image in assets/publications if it already exists
# - otherwise keep remote thumbnail URL as-is
# - otherwise fallback to PDF render
# ---------------------------------------------------------------------
def title_slug(title: str) -> str:
    return slugify(title)


def find_existing_local_thumbnail(title: str) -> str:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    slug = title_slug(title)

    print("\n-----------------------------")
    print(f"[thumb] TITLE = {title}")
    print(f"[thumb] SLUG  = {slug}")

    exact_candidates = []
    loose_candidates = []

    for p in THUMB_DIR.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            continue

        name = p.stem.lower()
        if name == slug or name.startswith(slug + "-") or ("-" + slug + "-") in ("-" + name + "-"):
            exact_candidates.append(p)
        elif slug and slug in name:
            loose_candidates.append(p)

    candidates = exact_candidates or loose_candidates

    print(f"[thumb] candidats = {[p.name for p in candidates]}")

    if not candidates:
        print("[thumb] aucune image locale trouvée")
        return ""

    candidates.sort(key=lambda p: (len(p.stem), p.name))
    chosen = candidates[0]

    print(f"[thumb] image retenue = {chosen.name}")
    return chosen.as_posix()


def extract_remote_thumbnail_url(dl, base_url: str) -> str:
    a = dl.select_one("dd.Vignette a")
    if a and a.get("href"):
        return urljoin(base_url, a["href"])

    img = dl.select_one("dd.Vignette img")
    if img and img.get("src"):
        return urljoin(base_url, img["src"])

    return ""


def render_pdf_first_page_thumbnail(pdf_url: str, title: str, session: requests.Session) -> str:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    existing = find_existing_local_thumbnail(title)
    if existing:
        return existing

    out_path = THUMB_DIR / f"{title_slug(title)}.jpg"

    try:
        print(f"[thumb] fallback PDF = {pdf_url}")
        r = session.get(pdf_url, headers=HEADERS, timeout=45)
        r.raise_for_status()

        doc = fitz.open(stream=r.content, filetype="pdf")
        if len(doc) == 0:
            return ""

        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        with open(out_path, "wb") as f:
            f.write(pix.tobytes("jpg"))
        doc.close()

        print(f"[thumb] vignette PDF générée = {out_path}")
        return out_path.as_posix()
    except Exception as e:
        print(f"[thumb] PDF thumbnail failed for {pdf_url}: {e}")
        return ""


def extract_thumbnail(dl, base_url: str, pdf_url: str, title: str, session: requests.Session) -> str:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    existing = find_existing_local_thumbnail(title)
    if existing:
        print(f"[thumb] réutilise image locale : {existing}")
        return existing

    remote_thumb_url = extract_remote_thumbnail_url(dl, base_url)
    print(f"[thumb] vignette distante = {remote_thumb_url}")

    if remote_thumb_url:
        print("[thumb] utilise URL distante")
        return remote_thumb_url

    if pdf_url:
        local_thumb = render_pdf_first_page_thumbnail(pdf_url, title, session)
        if local_thumb:
            return local_thumb

    print("[thumb] aucune vignette trouvée")
    return ""


# ---------------------------------------------------------------------
# Publication parsing
# ---------------------------------------------------------------------
def parse_publication(dl, base_url: str, session: requests.Session):
    title, page_url = extract_title_and_url(dl, base_url)
    if not title:
        return None

    authors, authors_full = extract_authors(dl)
    venue = extract_venue(dl)
    year = get_year(venue)

    if not year:
        year = get_year(dl.get_text(" ", strip=True))

    pdf, video, bibtex_url, code = extract_links(dl, base_url)
    thumbnail = extract_thumbnail(dl, base_url, pdf, title, session)

    return {
        "title": title,
        "authors": authors,
        "authors_full": authors_full,
        "year": year,
        "venue": venue,
        "pdf": pdf,
        "video": video,
        "thumbnail": thumbnail,
        "url": page_url,
        "bibtex_url": bibtex_url,
        "code": code,
    }


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    response = session.get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")
    dls = soup.select("dl.NoticeRes, dl.NoticeResAvecVignette")
    print("Nombre de blocs <dl> :", len(dls))

    publications = []
    seen_titles = set()

    for i, dl in enumerate(dls, start=1):
        pub = parse_publication(dl, URL, session)
        if not pub:
            continue

        pub = ensure_publication_fields(pub)

        key = normalize_title(pub["title"])
        if key in seen_titles:
            continue
        seen_titles.add(key)

        publications.append(pub)

        if i % 25 == 0:
            print(f"Traitement : {i}/{len(dls)}")

    # Auto publications by normalized title
    pub_index = {
        normalize_title(p.get("title", "")): p
        for p in publications
    }

    # Manual publications override
    if MANUAL_FILE.exists():
        manual_pubs = load_json_file(MANUAL_FILE, [])

        for pub in manual_pubs:
            pub = ensure_publication_fields(pub)
            key = normalize_title(pub.get("title", ""))

            if key in pub_index:
                del pub_index[key]

            pub_index[key] = pub

    publications = list(pub_index.values())

    # GitHub enrichment with cache
    github_cache = load_json_file(GITHUB_CACHE_FILE, {})
    enrich_publications_with_github(publications, session, github_cache)
    save_json_file(GITHUB_CACHE_FILE, github_cache)

    publications.sort(
        key=lambda x: ((x.get("year") or 0), x.get("title", "").lower()),
        reverse=True
    )

    OUTPUT.write_text(
        json.dumps(publications, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"{len(publications)} publications écrites dans {OUTPUT}")
    print(f"Cache GitHub écrit dans {GITHUB_CACHE_FILE}")


if __name__ == "__main__":
    main()

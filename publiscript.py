#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup

URL = "https://morpheo.inrialpes.fr/people/Boyer/index.php?id=elements"
OUTPUT = Path("publications.json")
THUMB_DIR = Path("assets/publications")

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


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

    # enlève DOI
    s = re.sub(r"\?\s*10\.\S+\s*\?", "", s)
    s = re.sub(r"https?://dx\.doi\.org/\S+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bdoi\s*:\s*\S+", "", s, flags=re.IGNORECASE)

    # enlève pages
    s = re.sub(r",?\s*pp\.\s*\d+\s*-\s*\d+\.?", "", s, flags=re.IGNORECASE)
    s = re.sub(r",?\s*pp\.\s*\d+\.?", "", s, flags=re.IGNORECASE)

    # nettoyage final
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ", ", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+\.", ".", s)
    s = s.strip(" ,;")
    return s


def is_image_url(url: str) -> bool:
    low = (url or "").lower()
    return any(ext in low for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"])


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:80] if text else "paper"


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


def extract_links(dl, base_url: str):
    pdf = ""
    video = ""
    bibtex_url = ""

    for a in dl.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        low = href.lower()

        if not pdf and ".pdf" in low:
            pdf = href

        if not video and (
            ".mp4" in low
            or "youtube" in low
            or "youtu.be" in low
            or "vimeo" in low
        ):
            video = href

        if not bibtex_url and low.endswith("/bibtex"):
            bibtex_url = href

    # fallback : parfois la vidéo est en texte brut dans dd.video
    if not video:
        node = dl.select_one("dd.video, .video")
        if node:
            txt = clean(node.get_text())
            if txt.startswith("http"):
                video = txt

    return pdf, video, bibtex_url


def download_bytes(url: str, session: requests.Session, timeout: int = 45) -> bytes:
    r = session.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content


def render_pdf_first_page_thumbnail(pdf_url: str, title: str, session: requests.Session) -> str:
    try:
        from PIL import Image
        import io

        THUMB_DIR.mkdir(parents=True, exist_ok=True)

        h = hashlib.md5(pdf_url.encode("utf-8")).hexdigest()[:10]
        stem = f"{slugify(title)}-{h}"
        out_path = THUMB_DIR / f"{stem}.jpg"

        if out_path.exists():
            return str(out_path).replace("\\", "/")

        pdf_bytes = download_bytes(pdf_url, session, timeout=45)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(doc) == 0:
            return ""

        def is_hal_cover_page(page) -> bool:
            """
            Heuristique pour détecter une page de garde HAL.
            """
            text = page.get_text("text") or ""
            text_low = " ".join(text.lower().split())

            hal_markers = [
                "hal is a multi-disciplinary open access archive",
                "archive ouverte pluridisciplinaire hal",
                "hal open science",
                "hal author manuscript",
                "submitted on",
                "cel-",
                "hal-",
            ]

            score = sum(1 for marker in hal_markers if marker in text_low)

            # Cas fréquent : présence forte de HAL dans le texte
            if score >= 2:
                return True

            # Fallback : très forte occurrence de "hal"
            if text_low.count("hal") >= 3 and (
                "archive ouverte" in text_low or "open access archive" in text_low
            ):
                return True

            return False

        start_page = 0
        if len(doc) > 1 and is_hal_cover_page(doc[0]):
            start_page = 1

        first_img_bytes = None

        for page_index in range(start_page, len(doc)):
            page = doc[page_index]
            images = page.get_images(full=True)
            if not images:
                continue

            # Première image trouvée sur cette page
            for img in images:
                xref = img[0]
                try:
                    img_info = doc.extract_image(xref)
                    img_bytes = img_info.get("image")
                    if img_bytes:
                        first_img_bytes = img_bytes
                        break
                except Exception:
                    continue

            if first_img_bytes is not None:
                break

        doc.close()

        if first_img_bytes is None:
            return ""

        img = Image.open(io.BytesIO(first_img_bytes)).convert("RGB")
        img.thumbnail((400, 400), Image.LANCZOS)
        img.save(out_path, format="JPEG", quality=85)

        return str(out_path).replace("\\", "/")

    except Exception:
        return ""
def extract_thumbnail(dl, base_url: str, pdf_url: str, title: str, session: requests.Session) -> str:
    # 1. meilleure option : lien parent de la vignette s'il pointe vers une vraie image
    a = dl.select_one("dd.Vignette a")
    if a and a.get("href"):
        href = urljoin(base_url, a["href"])
        if is_image_url(href):
            return href

    # 2. sinon, si l'image affichée n'est pas un simple thumb HAL, on la garde
    img = dl.select_one("dd.Vignette img")
    if img and img.get("src"):
        src = urljoin(base_url, img["src"])
        if is_image_url(src) and "/thumb/" not in src:
            return src

    # 3. sinon, génère une vignette locale depuis la première page du PDF
    if pdf_url:
        local_thumb = render_pdf_first_page_thumbnail(pdf_url, title, session)
        if local_thumb:
            return local_thumb

    # 4. dernier recours : miniature HAL si disponible
    if img and img.get("src"):
        return urljoin(base_url, img["src"])

    return ""


def parse_publication(dl, base_url: str, session: requests.Session):
    title, page_url = extract_title_and_url(dl, base_url)
    if not title:
        return None

    authors, authors_full = extract_authors(dl)
    venue = extract_venue(dl)
    year = get_year(venue)

    if not year:
        year = get_year(dl.get_text(" ", strip=True))

    pdf, video, bibtex_url = extract_links(dl, base_url)
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
    }


def main():
    session = requests.Session()

    response = session.get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")

    # Chaque publication correspond à un bloc <dl class="NoticeRes...">
    dls = soup.select("dl.NoticeRes, dl.NoticeResAvecVignette")
    print("Nombre de blocs <dl> :", len(dls))

    publications = []
    seen_titles = set()

    for i, dl in enumerate(dls, start=1):
        pub = parse_publication(dl, URL, session)
        if not pub:
            continue

        key = pub["title"].strip().lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)

        publications.append(pub)

        if i % 25 == 0:
            print(f"Traitement : {i}/{len(dls)}")

    publications.sort(
        key=lambda x: ((x["year"] or 0), x["title"].lower()),
        reverse=True
    )

    OUTPUT.write_text(
        json.dumps(publications, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"{len(publications)} publications écrites dans {OUTPUT}")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = "https://morpheo.inrialpes.fr/people/Boyer/index.php?id=elements"
OUTPUT = Path("publications.json")

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

    # enlever DOI
    s = re.sub(r"\?\s*10\.\S+\s*\?", "", s)
    s = re.sub(r"https?://dx\.doi\.org/\S+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bdoi\s*:\s*\S+", "", s, flags=re.IGNORECASE)

    # enlever pages
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
    low = url.lower()
    return any(ext in low for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"])


def extract_thumbnail(dl, base_url: str) -> str:
    # Meilleure qualité : le lien parent de la vignette
    a = dl.select_one("dd.Vignette a")
    if a and a.get("href"):
        href = urljoin(base_url, a["href"])
        if is_image_url(href):
            return href

    # Fallback : miniature affichée
    img = dl.select_one("dd.Vignette img")
    if img and img.get("src"):
        return urljoin(base_url, img["src"])

    return ""


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

    # fallback : la vidéo est parfois dans un dd.video comme texte brut
    if not video:
        node = dl.select_one("dd.video, .video")
        if node:
            txt = clean(node.get_text())
            if txt.startswith("http"):
                video = txt

    return pdf, video, bibtex_url


def parse_publication(dl, base_url: str):
    title, page_url = extract_title_and_url(dl, base_url)
    if not title:
        return None

    authors, authors_full = extract_authors(dl)
    venue = extract_venue(dl)
    year = get_year(venue)

    if not year:
        year = get_year(dl.get_text(" ", strip=True))

    thumbnail = extract_thumbnail(dl, base_url)
    pdf, video, bibtex_url = extract_links(dl, base_url)

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
    response = requests.get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")

    # Chaque publication est un <dl class="NoticeRes...">
    dls = soup.select("dl.NoticeRes, dl.NoticeResAvecVignette")
    print("Nombre de blocs <dl> :", len(dls))

    publications = []
    seen_titles = set()

    for i, dl in enumerate(dls, start=1):
        pub = parse_publication(dl, URL)
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
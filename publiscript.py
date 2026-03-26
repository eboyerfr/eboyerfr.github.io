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


def clean(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def get_year(text):
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", text or "")
    return int(years[-1]) if years else None


def shorten_authors(authors, max_authors=5):
    parts = [a.strip() for a in authors.split(",")]
    if len(parts) <= max_authors:
        return ", ".join(parts)
    return ", ".join(parts[:max_authors]) + ", et al."


def parse_publication(dl, base_url):
    title = ""
    authors = ""
    venue = ""
    pdf = ""
    video = ""
    thumbnail = ""
    page_url = ""

    # --- vignette
    img = dl.select_one("dd.Vignette img")
    if img and img.get("src"):
        thumbnail = urljoin(base_url, img["src"])

    # --- titre
    title_node = dl.select_one(".Titre a")
    if title_node:
        title = clean(title_node.get_text())
        page_url = title_node.get("href", "")

    # --- auteurs
    authors_node = dl.select_one(".Auteurs")
    if authors_node:
        authors = shorten_authors(clean(authors_node.get_text()))

    # --- venue
    venue_node = dl.select_one(".article")
    if venue_node:
        venue = clean(venue_node.get_text())

    # --- pdf / video
    for a in dl.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        low = href.lower()

        if not pdf and ".pdf" in low:
            pdf = href

        if not video and ("youtube" in low or "video" in low or ".mp4" in low):
            video = href

    # --- fallback video (cas spécial)
    video_node = dl.select_one(".video")
    if video_node and not video:
        video = clean(video_node.get_text())

    # --- année
    year = get_year(venue)

    if not title:
        return None

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "pdf": pdf,
        "video": video,
        "thumbnail": thumbnail,
        "url": page_url,
    }


def main():
    r = requests.get(URL, headers=HEADERS, timeout=60)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # 🔥 clé : on prend tous les <dl>
    dls = soup.select("dl.NoticeRes, dl.NoticeResAvecVignette")

    print("Nombre de blocs <dl>:", len(dls))

    publications = []
    seen = set()

    for dl in dls:
        pub = parse_publication(dl, URL)
        if not pub:
            continue

        key = pub["title"].lower()
        if key in seen:
            continue
        seen.add(key)

        publications.append(pub)

    publications.sort(
        key=lambda x: (x["year"] or 0),
        reverse=True
    )

    OUTPUT.write_text(
        json.dumps(publications, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"{len(publications)} publications écrites dans {OUTPUT}")


if __name__ == "__main__":
    main()
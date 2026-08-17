"""Build a large local corpus from the public-domain 1862 edition of Dal.

The book contains many thousands of proverb-like entries. This script uses
Wikimedia's MediaWiki API to enumerate the chapter pages, extracts ordinary
text lines, normalizes pre-reform spelling, removes obvious navigation/metadata
and exact duplicates, then writes data/dal_corpus.json.

It intentionally keeps the raw historical wording in `proverb` and does not
invent meanings. Semantic enrichment can be added later in a separate pass.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://ru.wikisource.org/w/api.php"
PREFIX = "Пословицы русского народа (Даль)/Изд. 1862 (ДО)/"
OUT = Path(__file__).resolve().parent / "dal_corpus.json"


def api(params):
    params = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "ChatterAi/1.0 proverb corpus builder"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def normalize(text):
    # Convert common pre-reform letters used by the 1862 edition.
    table = str.maketrans({"ѣ": "е", "Ѣ": "Е", "і": "и", "І": "И", "ѳ": "ф", "Ѳ": "Ф", "ѵ": "и", "Ѵ": "И"})
    text = text.translate(table).replace("ъ", "").replace("Ъ", "")
    text = re.sub(r"\s+", " ", text).strip(" \t-*•;,: ")
    return text


def is_candidate(line):
    line = normalize(line)
    if not line or len(line) < 8 or len(line) > 350:
        return False
    bad = (
        "Страница:", "Источник", "Примечан", "Опубл", "автор", "Содержание",
        "Image", "ISBN", "Google", "Викитека", "Назад", "Вперед", "←", "→",
    )
    if line.startswith(bad):
        return False
    if line.startswith(("{{", "[[", "==", "#", "<", "|")):
        return False
    # Most entries are short sayings, not prose paragraphs.
    words = line.split()
    if len(words) > 35:
        return False
    return True


def main():
    pages = []
    cont = {}
    while True:
        data = api({"action": "query", "list": "allpages", "apprefix": PREFIX, "aplimit": "max", **cont})
        pages.extend(p["title"] for p in data["query"]["allpages"])
        if "continue" not in data:
            break
        cont = data["continue"]

    records = []
    seen = set()
    for index, title in enumerate(pages, 1):
        try:
            data = api({"action": "parse", "page": title, "prop": "wikitext"})
            raw = data.get("parse", {}).get("wikitext", {}).get("*") or data.get("parse", {}).get("wikitext", "")
            # Remove templates, references and wiki links while preserving text.
            raw = re.sub(r"<ref[^>]*>.*?</ref>", " ", raw, flags=re.S)
            raw = re.sub(r"\{\{.*?\}\}", " ", raw, flags=re.S)
            raw = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", raw)
            raw = re.sub(r"'''?|''", "", raw)
            for line in raw.splitlines():
                line = normalize(line)
                if not is_candidate(line):
                    continue
                key = line.casefold()
                if key in seen:
                    continue
                seen.add(key)
                records.append({
                    "id": f"dal-{len(records)+1:06d}",
                    "proverb": line,
                    "type": "кандидат",
                    "meaning": "",
                    "topics": [],
                    "situations": [],
                    "keywords": [],
                    "variants": [],
                    "source": "В. И. Даль, «Пословицы русского народа», 1862",
                    "source_url": "https://ru.wikisource.org/wiki/Пословицы_русского_народа_(Даль)/Изд._1862_(ДО)",
                    "chapter": title.rsplit("/", 1)[-1],
                })
        except Exception as exc:
            print(f"skip {title}: {exc}")
        if index % 25 == 0:
            print(f"processed {index}/{len(pages)} pages; records={len(records)}")
        time.sleep(0.05)

    OUT.write_text(json.dumps({"source":"Даль 1862","count":len(records),"proverbs":records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}: {len(records)} records")


if __name__ == "__main__":
    main()

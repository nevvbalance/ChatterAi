"""Build a large Russian proverb corpus from public-domain Dal editions.

Sources currently included:
- Dal, 1862 edition on Wikisource.
- Dal, 1879 second edition, volumes 1 and 2 where page text is available.

The collector keeps the historical wording, records its source, normalizes
common pre-reform letters for duplicate detection, and does not invent meanings.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://ru.wikisource.org/w/api.php"
OUT = Path(__file__).resolve().parent / "dal_corpus.json"
PREFIXES = [
    ("1862", "Пословицы русского народа (Даль)/Изд. 1862 (ДО)/", "В. И. Даль, «Пословицы русского народа», 1862"),
    ("1879-t1", "Страница:Пословицы русского народа (1879, 2-е изд., том 1).pdf/", "В. И. Даль, «Пословицы русского народа», 1879, 2-е изд., том 1"),
    ("1879-t2", "Страница:Пословицы русского народа (1879, 2-е изд., том 2).pdf/", "В. И. Даль, «Пословицы русского народа», 1879, 2-е изд., том 2"),
]


def api(params):
    params = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "ChatterAi/1.0 proverb corpus builder"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def normalize(text):
    table = str.maketrans({
        "ѣ": "е", "Ѣ": "Е", "і": "и", "І": "И", "ѳ": "ф", "Ѳ": "Ф",
        "ѵ": "и", "Ѵ": "И",
    })
    text = text.translate(table)
    text = text.replace("ъ", "").replace("Ъ", "")
    text = re.sub(r"\s+", " ", text).strip(" \t-*•;,: ")
    return text


def clean_wikitext(raw):
    raw = re.sub(r"<ref[^>]*>.*?</ref>", " ", raw, flags=re.S)
    raw = re.sub(r"\{\{.*?\}\}", " ", raw, flags=re.S)
    raw = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", raw)
    raw = re.sub(r"'''?|''", "", raw)
    return raw


def is_candidate(line):
    line = normalize(line)
    if not line or len(line) < 8 or len(line) > 350:
        return False
    bad = (
        "Страница:", "Источник", "Примечан", "Опубл", "автор", "Содержание",
        "Image", "ISBN", "Google", "Викитека", "Назад", "Вперед", "←", "→",
    )
    if line.startswith(bad) or line.startswith(("{{", "[[", "==", "#", "<", "|")):
        return False
    return len(line.split()) <= 35


def get_pages(prefix):
    pages = []
    cont = {}
    while True:
        data = api({"action": "query", "list": "allpages", "apprefix": prefix, "aplimit": "max", **cont})
        pages.extend(p["title"] for p in data["query"]["allpages"])
        if "continue" not in data:
            return pages
        cont = data["continue"]


def collect(source_id, prefix, source_name, records, seen):
    pages = get_pages(prefix)
    print(f"{source_id}: {len(pages)} pages")
    for index, title in enumerate(pages, 1):
        try:
            data = api({"action": "parse", "page": title, "prop": "wikitext"})
            raw = data.get("parse", {}).get("wikitext", {}).get("*") or data.get("parse", {}).get("wikitext", "")
            for line in clean_wikitext(raw).splitlines():
                line = normalize(line)
                if not is_candidate(line):
                    continue
                key = re.sub(r"[^а-яёa-z0-9]+", " ", line.casefold()).strip()
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
                    "source": source_name,
                    "source_url": "https://ru.wikisource.org/wiki/Пословицы_русского_народа_(Даль)",
                    "source_page": title,
                    "source_id": source_id,
                })
        except Exception as exc:
            print(f"skip {title}: {exc}")
        if index % 50 == 0:
            print(f"{source_id}: processed {index}/{len(pages)}; records={len(records)}")
        time.sleep(0.05)


def main():
    records = []
    seen = set()
    for source_id, prefix, source_name in PREFIXES:
        collect(source_id, prefix, source_name, records, seen)

    payload = {
        "dataset": "ChatterAi Dal corpus",
        "version": 2,
        "count": len(records),
        "sources": [name for _, _, name in PREFIXES],
        "proverbs": records,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}: {len(records)} unique records")


if __name__ == "__main__":
    main()

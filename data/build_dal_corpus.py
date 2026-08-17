"""Build a Russian proverb corpus from Wikisource's Dal collection."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://ru.wikisource.org/w/api.php"
OUT = Path(__file__).resolve().parent / "dal_corpus.json"
PREFIX = "Пословицы русского народа (Даль)/Изд. 1862 (ДО)/"
SOURCE = "В. И. Даль, «Пословицы русского народа», 1862"
SOURCE_URL = "https://ru.wikisource.org/wiki/Пословицы_русского_народа_(Даль)/Изд._1862_(ДО)"


def api(params):
    params = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "ChatterAi corpus builder/1.0"})
    with urlopen(req, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


def normalize(text):
    text = text.translate(str.maketrans({"ѣ":"е","Ѣ":"Е","і":"и","І":"И","ѳ":"ф","Ѳ":"Ф","ѵ":"и","Ѵ":"И"}))
    text = text.replace("ъ", "").replace("Ъ", "")
    return re.sub(r"\s+", " ", text).strip(" \t-*•;,: ")


def clean(raw):
    raw = re.sub(r"<ref[^>]*>.*?</ref>", " ", raw, flags=re.S)
    raw = re.sub(r"\{\{.*?\}\}", " ", raw, flags=re.S)
    raw = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", raw)
    raw = re.sub(r"'''?|''", "", raw)
    return raw


def candidate(line):
    line = normalize(line)
    if not (8 <= len(line) <= 350) or len(line.split()) > 35:
        return None
    bad = ("Страница:", "Источник", "Примечан", "Содержание", "Викитека", "ISBN", "Google", "Image", "←", "→")
    if line.startswith(bad) or line.startswith(("{{", "[[", "==", "#", "<", "|")):
        return None
    return line if re.search(r"[А-Яа-яЁё]", line) else None


def get_pages():
    pages, cont = [], {}
    while True:
        data = api({"action":"query", "list":"allpages", "apprefix":PREFIX, "aplimit":"max", **cont})
        batch = data.get("query", {}).get("allpages", [])
        pages.extend(p["title"] for p in batch)
        if not data.get("continue"):
            break
        cont = data["continue"]
    return pages


def get_text(title):
    data = api({"action":"parse", "page":title, "prop":"wikitext"})
    raw = data.get("parse", {}).get("wikitext", "")
    return raw if isinstance(raw, str) else raw.get("*", "")


def main():
    pages = get_pages()
    if not pages:
        raise RuntimeError("Wikisource returned zero pages for the configured Dal prefix")
    records, seen = [], set()
    processed = 0
    duplicates = 0
    for title in pages:
        raw = get_text(title)
        processed += 1
        for raw_line in clean(raw).splitlines():
            line = candidate(raw_line)
            if not line:
                continue
            key = re.sub(r"[^а-яёa-z0-9]+", " ", line.casefold()).strip()
            if key in seen:
                duplicates += 1
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
                "source": SOURCE,
                "source_url": SOURCE_URL,
                "source_page": title,
            })
        if processed % 25 == 0:
            print(f"pages={processed}/{len(pages)}, unique_records={len(records)}, duplicates={duplicates}")
        time.sleep(0.05)
    if not records:
        raise RuntimeError(f"Corpus build found 0 records after processing {processed} pages")
    OUT.write_text(json.dumps({
        "dataset":"ChatterAi Dal corpus", "version":4,
        "count":len(records), "pages_processed":processed,
        "duplicates_removed":duplicates, "proverbs":records
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SUCCESS: pages={processed}; unique_records={len(records)}; duplicates={duplicates}")


if __name__ == "__main__":
    main()

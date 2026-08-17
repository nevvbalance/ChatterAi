"""Build a large Russian proverb corpus from public-domain Dal editions."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://ru.wikisource.org/w/api.php"
OUT = Path(__file__).resolve().parent / "dal_corpus.json"
# Start with the verified 1862 namespace. More editions can be added after
# their exact Wikisource page prefixes are verified.
PREFIXES = [
    ("1862", "Пословицы русского народа (Даль)/Изд. 1862 (ДО)/", "В. И. Даль, «Пословицы русского народа», 1862"),
]


def api(params):
    params = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "ChatterAi/1.0 proverb corpus builder"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def normalize(text):
    table = str.maketrans({"ѣ": "е", "Ѣ": "Е", "і": "и", "І": "И", "ѳ": "ф", "Ѳ": "Ф", "ѵ": "и", "Ѵ": "И"})
    text = text.translate(table).replace("ъ", "").replace("Ъ", "")
    return re.sub(r"\s+", " ", text).strip(" \t-*•;,: ")


def clean_wikitext(raw):
    raw = re.sub(r"<ref[^>]*>.*?</ref>", " ", raw, flags=re.S)
    raw = re.sub(r"\{\{.*?\}\}", " ", raw, flags=re.S)
    raw = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", raw)
    raw = re.sub(r"'''?|''", "", raw)
    return raw


def is_candidate(line):
    line = normalize(line)
    if not line or len(line) < 8 or len(line) > 350 or len(line.split()) > 35:
        return False
    bad = ("Страница:", "Источник", "Примечан", "Опубл", "автор", "Содержание", "Image", "ISBN", "Google", "Викитека", "Назад", "Вперед", "←", "→")
    return not line.startswith(bad) and not line.startswith(("{{", "[[", "==", "#", "<", "|"))


def get_pages(prefix):
    pages = []
    cont = {}
    while True:
        data = api({"action": "query", "list": "allpages", "apprefix": prefix, "aplimit": "max", **cont})
        if "query" not in data:
            raise RuntimeError(f"Wikisource API error: {data.get('error', data)}")
        pages.extend(p["title"] for p in data["query"]["allpages"])
        if "continue" not in data:
            return pages
        cont = data["continue"]


def collect(source_id, prefix, source_name, records, seen):
    pages = get_pages(prefix)
    print(f"{source_id}: {len(pages)} pages")
    for index, title in enumerate(pages, 1):
        data = api({"action": "parse", "page": title, "prop": "wikitext"})
        parsed = data.get("parse", {})
        raw = parsed.get("wikitext", "")
        # formatversion=2 returns wikitext as a string. Older responses may
        # return an object with a '*' field, so support both forms.
        if isinstance(raw, dict):
            raw = raw.get("*", "")
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
        if index % 50 == 0:
            print(f"{source_id}: processed {index}/{len(pages)}; records={len(records)}")
        time.sleep(0.05)


def main():
    records, seen = [], set()
    for source_id, prefix, source_name in PREFIXES:
        collect(source_id, prefix, source_name, records, seen)
    OUT.write_text(json.dumps({
        "dataset": "ChatterAi Dal corpus",
        "version": 3,
        "count": len(records),
        "sources": [name for _, _, name in PREFIXES],
        "proverbs": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}: {len(records)} unique records")


if __name__ == "__main__":
    main()

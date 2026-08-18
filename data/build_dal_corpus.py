"""Build a cleaner Russian proverb candidate corpus from Dal pages."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://ru.wikisource.org/w/api.php"
OUT = Path(__file__).resolve().parent / "dal_corpus.json"
CATEGORIES = ["Категория:Пословицы русского народа (Даль)", "Категория:Пословицы русского народа (Даль)/ДО"]
SOURCE = "В. И. Даль, «Пословицы русского народа», 1862 / Wikisource"
SOURCE_URL = "https://ru.wikisource.org/wiki/Категория:Пословицы_русского_народа_(Даль)"
NOISE = re.compile(r"^(?:в\s+викитеке|переиздания\s+в\s+современной\s+орфографии|содержание|примечания?|источник|литература|см\.|назад|впер[её]д|автор|редактор|издание|категория|страница|текст|оглавление)\b", re.I)


def api(params):
    params = {**params, "format": "json", "formatversion": "2"}
    req = Request(API + "?" + urlencode(params), headers={"User-Agent": "ChatterAi corpus builder/2.1"})
    with urlopen(req, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


def normalize(text):
    table = str.maketrans({"ѣ":"е","Ѣ":"Е","і":"и","І":"И","ѳ":"ф","Ѳ":"Ф","ѵ":"и","Ѵ":"И"})
    text = text.translate(table).replace("ъ", "").replace("Ъ", "")
    return re.sub(r"\s+", " ", text).strip(" \t-*•;,: ")


def clean(raw):
    raw = re.sub(r"<ref[^>]*>.*?</ref>", " ", raw, flags=re.S)
    raw = re.sub(r"\{\{.*?\}\}", " ", raw, flags=re.S)
    raw = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"'''?|''", "", raw)
    return raw


def candidate(line):
    line = normalize(line)
    if not 10 <= len(line) <= 300 or not 2 <= len(line.split()) <= 30:
        return None
    if NOISE.search(line) or line.startswith(("{{", "[[", "==", "#", "<", "|", "http")):
        return None
    if not re.search(r"[А-Яа-яЁё]{3,}", line):
        return None
    # Reject obvious editorial/navigation fragments.
    if re.search(r"\b(?:переиздани|викитек|современн(?:ой|ая)\s+орфограф|ISBN|Google)\b", line, re.I):
        return None
    return line


def category_members(category):
    pages, cont = [], {}
    while True:
        data = api({"action":"query", "list":"categorymembers", "cmtitle":category, "cmnamespace":"0", "cmlimit":"max", **cont})
        pages.extend(p["title"] for p in data.get("query", {}).get("categorymembers", []))
        if not data.get("continue"):
            return pages
        cont = data["continue"]


def get_text(title):
    data = api({"action":"parse", "page":title, "prop":"wikitext"})
    raw = data.get("parse", {}).get("wikitext", "")
    return raw if isinstance(raw, str) else raw.get("*", "")


def main():
    pages = list(dict.fromkeys(sum((category_members(c) for c in CATEGORIES), [])))
    if not pages:
        raise RuntimeError("Wikisource returned zero Dal pages")
    records, seen = [], set(); duplicates = rejected = 0
    for i, title in enumerate(pages, 1):
        try:
            raw = get_text(title)
        except Exception as exc:
            print(f"SKIP {title}: {exc}"); continue
        for raw_line in clean(raw).splitlines():
            line = candidate(raw_line)
            if not line: rejected += 1; continue
            key = re.sub(r"[^а-яёa-z0-9]+", " ", line.casefold()).strip()
            if key in seen: duplicates += 1; continue
            seen.add(key)
            records.append({"id": f"dal-{len(records)+1:06d}", "proverb": line, "type": "кандидат", "meaning": "", "topics": [], "situations": [], "keywords": [], "variants": [], "source": SOURCE, "source_url": SOURCE_URL, "source_page": title})
        if i % 20 == 0: print(f"pages={i}/{len(pages)} records={len(records)} duplicates={duplicates} rejected={rejected}")
        time.sleep(0.03)
    if not records: raise RuntimeError(f"Corpus build found 0 candidates after processing {len(pages)} pages")
    OUT.write_text(json.dumps({"dataset":"ChatterAi Dal corpus", "version":7, "count":len(records), "pages_processed":len(pages), "duplicates_removed":duplicates, "rejected_lines":rejected, "proverbs":records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SUCCESS: pages={len(pages)} records={len(records)} duplicates={duplicates} rejected={rejected}")

if __name__ == "__main__": main()

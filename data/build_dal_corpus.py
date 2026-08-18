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


def api(params, retries=6):
    params = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urlencode(params)
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "ChatterAi corpus builder/3.0 (educational research)"})
            with urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            if "error" in data:
                raise RuntimeError(data["error"])
            return data
        except Exception as exc:
            if attempt == retries - 1:
                raise
            delay = min(30, 3 * (2 ** attempt))
            print(f"API retry {attempt + 1}/{retries} after {type(exc).__name__}: sleeping {delay}s")
            time.sleep(delay)


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


def get_texts(titles):
    """Fetch up to 50 pages in one API request, avoiding hundreds of calls."""
    result = {}
    for start in range(0, len(titles), 50):
        chunk = titles[start:start + 50]
        data = api({"action":"query", "prop":"revisions", "rvprop":"content", "rvslots":"main",
                    "titles":"|".join(chunk)})
        for page in data.get("query", {}).get("pages", []):
            title = page.get("title")
            revisions = page.get("revisions", [])
            if not title or not revisions:
                continue
            slots = revisions[0].get("slots", {})
            main = slots.get("main", {})
            result[title] = main.get("content", main.get("*", ""))
        if start + 50 < len(titles):
            time.sleep(1.5)
    return result


def main():
    pages = list(dict.fromkeys(sum((category_members(c) for c in CATEGORIES), [])))
    if not pages:
        raise RuntimeError("Wikisource returned zero Dal pages")
    print(f"Discovered pages: {len(pages)}")
    texts = get_texts(pages)
    print(f"Fetched page texts: {len(texts)}/{len(pages)}")

    records, seen = [], set(); duplicates = rejected = 0
    for i, title in enumerate(pages, 1):
        raw = texts.get(title, "")
        if not raw:
            continue
        for raw_line in clean(raw).splitlines():
            line = candidate(raw_line)
            if not line:
                rejected += 1
                continue
            key = re.sub(r"[^а-яёa-z0-9]+", " ", line.casefold()).strip()
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            records.append({"id": f"dal-{len(records)+1:06d}", "proverb": line, "type": "кандидат", "meaning": "", "topics": [], "situations": [], "keywords": [], "variants": [], "source": SOURCE, "source_url": SOURCE_URL, "source_page": title})
        if i % 50 == 0:
            print(f"pages={i}/{len(pages)} records={len(records)} duplicates={duplicates} rejected={rejected}")

    if not records:
        raise RuntimeError(f"Corpus build found 0 candidates after processing {len(pages)} pages")
    OUT.write_text(json.dumps({"dataset":"ChatterAi Dal corpus", "version":8, "count":len(records), "pages_processed":len(pages), "pages_fetched":len(texts), "duplicates_removed":duplicates, "rejected_lines":rejected, "proverbs":records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SUCCESS: pages={len(pages)} fetched={len(texts)} records={len(records)} duplicates={duplicates} rejected={rejected}")


if __name__ == "__main__":
    main()

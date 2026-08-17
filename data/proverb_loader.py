import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_proverbs():
    """Load the main corpus plus all candidate batches and remove exact duplicates."""
    records = []
    seen = set()

    main_path = DATA_DIR / "russian_proverbs.json"
    if main_path.exists():
        data = _load_json(main_path)
        for item in data.get("proverbs", []):
            _add(records, seen, item)

    for path in sorted(DATA_DIR.glob("russian_proverbs_candidates_*.json")):
        data = _load_json(path)
        for item in data.get("items", []):
            proverb = item.get("proverb", "").strip()
            if not proverb:
                continue
            normalized = {
                "id": item.get("id", path.stem),
                "proverb": proverb,
                "type": "кандидат",
                "meaning": "",
                "topics": [item.get("section", "").replace(" — ", ", ")] if item.get("section") else [],
                "situations": [],
                "keywords": proverb.lower().replace("—", " ").replace(",", " ").replace(".", " ").split(),
                "variants": [],
                "source": data.get("source", "") or "Даль, 1862",
            }
            _add(records, seen, normalized)

    return records


def _add(records, seen, item):
    key = " ".join(item.get("proverb", "").lower().split())
    if key and key not in seen:
        seen.add(key)
        records.append(item)


def search_proverbs(query: str, limit: int = 5):
    """Simple offline semantic-ish ranking using words, topics, situations and meaning."""
    words = {w.lower().strip(".,!?;:()\"'«»") for w in query.split() if len(w) >= 3}
    if not words:
        return []

    results = []
    for item in load_proverbs():
        haystack = " ".join([
            item.get("proverb", ""),
            item.get("meaning", ""),
            " ".join(item.get("topics", [])),
            " ".join(item.get("situations", [])),
            " ".join(item.get("keywords", [])),
        ]).lower()
        score = sum(1 for word in words if word in haystack)
        if score:
            results.append((score, item))

    results.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in results[:limit]]

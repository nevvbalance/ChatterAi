"""Deterministic first-pass classifier for the collected Russian folklore corpus.

It does not claim that every classification is perfect. It assigns a conservative
kind using lexical signals and marks uncertain records for later review.
"""
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent / "dal_corpus.json"
OUT = Path(__file__).resolve().parent / "proverbs.json"

RULES = {
    "загадка": ["что это", "кто это", "загадка"],
    "примета": ["примета", "к чему", "если ворон", "если сорока", "если кошка"],
    "игровое выражение": ["считалка", "жеребий", "игра", "конанье"],
    "поговорка": ["говорят", "как говорится"],
}

TOPICS = {
    "труд": ["труд", "работ", "дело", "работать", "пахат", "ленив"],
    "дружба": ["друг", "дружб", "товарищ", "недруг", "приятел"],
    "семья": ["семь", "мать", "отец", "сын", "дочь", "муж", "жена", "родн"],
    "деньги": ["деньг", "богат", "бедн", "рубл", "копеек", "богач"],
    "правда и ложь": ["правд", "лж", "лож", "обман", "вран"],
    "ум и знания": ["ум", "разум", "знани", "уч", "мудр", "глуп"],
    "время": ["врем", "день", "год", "час", "век", "утр", "вечер"],
    "терпение": ["терп", "поспеш", "тороп", "жд", "медл"],
    "характер": ["нрав", "характер", "смел", "страх", "горд", "скром"],
}


def classify(text):
    low = text.casefold()
    for kind, signals in RULES.items():
        if any(s in low for s in signals):
            return kind, "rule"
    # Short aphoristic lines are retained as candidates for semantic review.
    return "пословица-кандидат", "uncertain"


def topics(text):
    low = text.casefold()
    return [topic for topic, signals in TOPICS.items() if any(s in low for s in signals)]


def keywords(text):
    words = re.findall(r"[а-яё]{4,}", text.casefold())
    stop = {"который", "которая", "которые", "чтобы", "если", "тогда", "этого", "этот", "такие", "такой"}
    return list(dict.fromkeys(w for w in words if w not in stop))[:20]


def main():
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    out = []
    for item in payload.get("proverbs", []):
        kind, confidence = classify(item["proverb"])
        out.append({
            **item,
            "type": kind,
            "classification_confidence": confidence,
            "topics": topics(item["proverb"]),
            "keywords": keywords(item["proverb"]),
            "meaning": item.get("meaning", ""),
            "review_required": confidence == "uncertain",
        })
    result = {
        "dataset": "ChatterAi Russian folklore corpus",
        "version": 1,
        "count": len(out),
        "proverbs": out,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}: {len(out)} records")


if __name__ == "__main__":
    main()

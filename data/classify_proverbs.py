"""Deterministic first-pass classifier for the growing Russian folklore corpus.

Classification is intentionally conservative. Uncertain records stay in the corpus and
are marked for later semantic review instead of being silently discarded.
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
    "любовь": ["любов", "любить", "сердц", "мил", "жених", "невест"],
    "жизнь": ["жизн", "смерт", "судьб", "долг", "старост", "молод"],
    "здоровье": ["здоров", "болезн", "болен", "лекар", "врач"],
    "природа": ["земл", "вод", "лес", "рек", "дожд", "солнц", "ветр", "мороз"],
    "дом и хозяйство": ["дом", "хозяй", "двор", "печ", "изб", "урож", "скот"],
}

SITUATIONS = {
    "совет": ["надо", "нужно", "следует", "не стоит", "берегись", "помни"],
    "предупреждение": ["бойся", "берегись", "не доверяй", "опас"],
    "дружба": ["друг", "дружб", "товарищ"],
    "работа": ["труд", "работ", "дело", "пахат"],
    "отношения": ["любов", "жена", "муж", "друг", "родн"],
    "деньги": ["деньг", "рубл", "богат", "бедн"],
    "ошибка": ["ошиб", "винов", "поспеш", "неразум"],
}

STOP = {"который", "которая", "которые", "чтобы", "если", "тогда", "этого", "этот", "такие", "такой"}


def classify(text):
    low = text.casefold()
    for kind, signals in RULES.items():
        if any(s in low for s in signals):
            return kind, "rule"
    return "пословица-кандидат", "uncertain"


def matched_groups(text, groups):
    low = text.casefold()
    return [name for name, signals in groups.items() if any(s in low for s in signals)]


def keywords(text):
    words = re.findall(r"[а-яё]{4,}", text.casefold())
    return list(dict.fromkeys(w for w in words if w not in STOP))[:20]


def main():
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    out = []
    for item in payload.get("proverbs", []):
        text = item["proverb"]
        kind, confidence = classify(text)
        out.append({
            **item,
            "type": kind,
            "classification_confidence": confidence,
            "topics": matched_groups(text, TOPICS),
            "situations": matched_groups(text, SITUATIONS),
            "keywords": keywords(text),
            "meaning": item.get("meaning", ""),
            "review_required": confidence == "uncertain" or not matched_groups(text, TOPICS),
        })
    result = {
        "dataset": "ChatterAi Russian folklore corpus",
        "version": 2,
        "count": len(out),
        "proverbs": out,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}: {len(out)} records")


if __name__ == "__main__": main()

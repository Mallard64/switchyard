"""Hard checks and gold scoring for spacy-llm outputs.

Each hard check names the prompt instruction it enforces, so a failure points at an
instruction (the cause finder builds on this). Gold scores are reported separately:
they measure accuracy, not whether the output is usable.
"""
import re

NER_LABELS = {"DISH", "INGREDIENT", "EQUIPMENT"}
TEXTCAT_LABELS = {"COMPLIMENT", "INSULT"}
PRONOUNS = {"i", "me", "you", "he", "him", "she", "her", "it", "we", "us", "they", "them",
            "this", "that", "these", "those", "mine", "yours", "his", "hers", "its", "ours", "theirs"}
# spacy.NER.v3 answer line: "1. <span> | True | LABEL | reason"
NER_LINE = re.compile(r"^\s*\d+\.\s*(?P<span>.+?)\s*\|\s*(?P<is>True|False)\s*\|\s*(?P<label>[^|]+?)\s*\|\s*(?P<reason>.*)$")

NER_CHECKS = {
    "format": "Only use this output format ... Do not output anything besides entities in this output format.",
    "valid_labels": "use the labels provided above (DISH, INGREDIENT, EQUIPMENT)",
    "spans_in_text": "identify a list of entities [in the paragraph]",
    "no_pronouns": "Pronouns are not entities.",
    "nonempty": "identify a list of entities",
    "in_order": "Output entities in the order they occur in the input paragraph regardless of label.",
}
TEXTCAT_CHECKS = {
    "valid_label": "Classify the text below to any of the following labels: COMPLIMENT, INSULT",
    "bare_answer": "Do not put any other text in your answer, only one of the provided labels",
    "single_label": "The task is exclusive, so only choose one label",
}


def _norm(s):
    return " ".join(s.lower().split())


def check_ner(text, raw, ents):
    """raw: model's response text. ents: [{"text","label"}] that spaCy kept on the Doc."""
    lines = [l for l in raw.strip().splitlines() if l.strip()]
    parsed = [NER_LINE.match(l) for l in lines]
    claimed = [m for m in parsed if m and m.group("is") == "True"]
    checks = {
        "format": bool(lines) and all(parsed),
        "valid_labels": all(m.group("label").strip() in NER_LABELS for m in claimed),
        "spans_in_text": all(_norm(m.group("span")) in _norm(text) for m in claimed),
        "no_pronouns": all(_norm(m.group("span")) not in PRONOUNS for m in claimed),
        "nonempty": bool(ents),
        "in_order": _in_order(text, [m.group("span") for m in claimed]),
    }
    return checks


def _in_order(text, spans):
    """Listed spans appear in the paragraph in the same order (spacy-llm drops out-of-order ones)."""
    low, pos = _norm(text), 0
    for s in spans:
        i = low.find(_norm(s), pos)
        if i == -1:
            if _norm(s) in low:  # occurs, but only earlier than the previous span
                return False
            continue  # not in text at all: spans_in_text covers that
        pos = i
    return True


def score_ner(ents, gold):
    pred = {(_norm(e["text"]), e["label"]) for e in ents}
    ref = {(_norm(g["text"]), g["label"]) for g in gold}
    tp = len(pred & ref)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(ref) if ref else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "n_pred": len(pred), "n_gold": len(ref), "precision": round(p, 4),
            "recall": round(r, 4), "f1": round(f, 4),
            "missed": sorted(ref - pred), "extra": sorted(pred - ref)}


def check_textcat(raw, cats):
    answer = raw.strip()
    core = answer.strip("`'\".:; ").upper()
    chosen = [k for k, v in cats.items() if v >= 0.5]
    return {
        "valid_label": core in TEXTCAT_LABELS,
        "bare_answer": answer.upper() in TEXTCAT_LABELS,
        "single_label": len(chosen) == 1,
    }


def score_textcat(cats, gold):
    chosen = [k for k, v in cats.items() if v >= 0.5]
    pred = chosen[0] if len(chosen) == 1 else None
    return {"pred": pred, "correct": pred == gold}

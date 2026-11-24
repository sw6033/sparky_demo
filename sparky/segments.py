"""Split spoken reply into natural TTS sentence chunks without breaking Chinese quotes."""
from __future__ import annotations


def weighted_len(text: str) -> int:
    return sum(3 if "\u3400" <= ch <= "\u9fff" else 1 for ch in text)


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts: list[str] = []
    start = 0
    in_quote = False
    for idx, ch in enumerate(text):
        if ch in '"“”「」『』':
            in_quote = not in_quote
        if in_quote or ch not in ".!?。！？":
            continue
        if ch == "." and idx > 0 and idx + 1 < len(text) and text[idx - 1].isdigit() and text[idx + 1].isdigit():
            continue
        if ch in ".!?" and idx + 1 < len(text) and not text[idx + 1].isspace():
            continue
        chunk = text[start : idx + 1].strip()
        if chunk:
            parts.append(chunk)
        start = idx + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)

    # Merge ultra-short opener like "Hey!" or "Ooh!" with the next sentence so TTS prosody flows naturally.
    if len(parts) > 1 and weighted_len(parts[0]) <= 16:
        parts[:2] = [f"{parts[0]} {parts[1]}".strip()]
    return parts


def make_segments(text: str) -> list[dict]:
    return [
        {"index": idx, "text": part, "delivery": "pending"}
        for idx, part in enumerate(split_sentences(text))
    ]

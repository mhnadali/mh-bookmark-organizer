"""Text normalisation shared by search, filing and duplicate detection.

Handles the usual traps of multilingual libraries: Arabic/Persian letter variants (ي/ی, ك/ک), the zero-width non-joiner,
diacritics, tatweel, Arabic-Indic digits and full-width forms.
"""
from __future__ import annotations

import re
import unicodedata

_TRANSLATE = str.maketrans({
    "ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "ٱ": "ا", "ؤ": "و",
    "‌": " ", "‍": "", "‎": "", "‏": "", "‪": "", "‫": "", "‬": "",
    "⁦": "", "⁧": "", "⁨": "", "⁩": "", "﻿": "",
})
_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGITS.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_MARKS = re.compile("[ً-ٰٟـ]")  # harakat, superscript alef, tatweel
_WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)
STOP = frozenset(["the", "and", "for", "with", "from", "that", "this", "you", "your", "are", "was", "how", "what", "why", "when", "into", "about", "http", "https", "www", "com", "org", "net", "html", "htm", "php", "index", "page", "home", "best", "new", "top"])


def normalize_text(text: str) -> str:
    """Lower-cased, NFKC-folded text with Arabic/Persian variants unified."""
    text = unicodedata.normalize("NFKC", text or "").lower().translate(_TRANSLATE).translate(_DIGITS)
    return _MARKS.sub("", text)


def words(text: str) -> list:
    return [w for w in _WORD.findall(normalize_text(text)) if w not in STOP and not w.isdigit()]


def char_ngrams(text: str, sizes=(3, 4)) -> list:
    joined = " ".join(words(text))
    return [joined[i:i + n] for n in sizes for i in range(len(joined) - n + 1)]

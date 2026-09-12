"""Japanese-aware tokenising for the places we count words.

The obvious pattern - ``[\\w぀-ヿ一-鿿]{2,}`` - looks like it tokenises
Japanese and does not. Japanese is written without spaces, so that pattern
matches a whole clause as one token: プロテインはいつ飲むんですか comes back
as a single "word", which can never agree with the same word in another
comment. Every frequency count built on it silently returns 1 for everything.

A morphological analyser (MeCab, Janome, SudachiPy) would segment this
properly, but all of them are heavy dependencies with dictionaries to ship,
and this codebase runs with none. The cheap approximation that actually works
for counting: **split on script boundaries**. Japanese marks its own content
words - they are written in kanji and katakana, while hiragana carries the
grammar (は, いつ, んですか). Taking maximal runs of each script gives
プロテイン and 飲 out of that clause, which is what a frequency count needs.

It is an approximation, and it under-segments compound kanji (筋力trainingの
ような複合語 stays whole). For ranking which terms recur across a corpus,
under-segmentation costs far less than treating every sentence as a unique
token.
"""

from __future__ import annotations

import re

# Maximal runs, per script. Order in the alternation does not matter because
# the classes are disjoint.
_TOKEN = re.compile(
    r"[ァ-ヺヽヾー]{2,}"          # katakana: loanwords, product and brand names
    r"|[一-鿿々]{2,}"             # kanji runs: most Japanese content words
    r"|[a-zA-Z][a-zA-Z0-9'\-]{1,}"  # latin words
)

# Counted separately: a single kanji is often a real word (糖, 筋, 肌) but also
# the tail of a verb stem, so it is only kept when nothing else was found.
_SINGLE_KANJI = re.compile(r"[一-鿿]")

STOP = {
    # Japanese function words that survive script-run splitting
    "これ", "それ", "あれ", "この", "その", "ため", "よう", "こと", "もの",
    "場合", "自分", "本当", "普通", "最近", "今回", "動画", "投稿",
    # English
    "the", "and", "for", "you", "your", "with", "this", "that", "are", "was",
    "how", "what", "why", "can", "have", "from", "but", "not", "all", "out",
}


def tokenize(text: str | None, min_length: int = 2) -> list[str]:
    """Content-word-ish tokens, lowercased, stop words removed."""
    if not text:
        return []
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    tokens = [t for t in tokens if len(t) >= min_length and not t.isdigit()]
    kept = [t for t in tokens if t not in STOP]
    if kept:
        return kept
    # Nothing survived - fall back to single kanji so a short comment like
    # 糖質? still contributes something countable.
    return [c for c in _SINGLE_KANJI.findall(text)]

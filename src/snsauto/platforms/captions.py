"""Fitting a caption into a platform's limit without breaking it.

Two things go wrong with the obvious ``text[:limit]``:

**It breaks hashtags and emoji.** Cutting mid-token turns ``#ダイエット`` into
``#ダイエ`` - a different, probably nonexistent hashtag that the post is now
filed under - and can split an emoji's zero-width-joiner sequence into
fragments that render as unrelated glyphs.

**X does not count characters the way Python does.** X uses a *weighted*
length: Latin and a few punctuation ranges count as 1, and everything else -
including all kana and kanji - counts as 2. A 280-character Japanese tweet is
560 weighted and the API rejects it. Measured in Python's ``len`` a Japanese
account effectively gets 140 characters, and building to 280 produces posts
that fail at publish time, after the video has already uploaded.
"""

from __future__ import annotations

import re

# X's own ranges that weigh 1. Everything outside them weighs 2.
# https://developer.x.com/en/docs/counting-characters
LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))

# A URL is always counted as this many, however long it is.
URL_WEIGHT = 23
URL_RE = re.compile(r"https?://\S+")

ZWJ = "‍"
# Variation selectors and skin-tone modifiers belong to the glyph before them.
# How much of the fitted text may be given up to land on a clean boundary.
# Beyond this the ragged edge is the lesser evil.
MAX_GIVEBACK = 0.25

COMBINING = re.compile(r"[︀-️\U0001F3FB-\U0001F3FF⃣]")


def weighted_length(text: str) -> int:
    """X's character count. Japanese weighs double; URLs are fixed at 23."""
    if not text:
        return 0
    total = 0
    remainder = URL_RE.sub("", text)
    total += URL_WEIGHT * len(URL_RE.findall(text))
    for char in remainder:
        code = ord(char)
        total += 1 if any(lo <= code <= hi for lo, hi in LIGHT_RANGES) else 2
    return total


def _is_safe_break(text: str, index: int) -> bool:
    """Can the string be cut here without splitting something meaningful?"""
    if index <= 0 or index >= len(text):
        return True
    before, after = text[index - 1], text[index]
    # Never split an emoji sequence.
    if before == ZWJ or after == ZWJ:
        return False
    if COMBINING.match(after):
        return False
    # Never split a hashtag or a mention: the truncated form is a different
    # tag, and the post gets filed under it.
    head = text[:index]
    token_start = max(head.rfind(" "), head.rfind("\n"), head.rfind("　"))
    token = head[token_start + 1:]
    if token.startswith(("#", "＃", "@", "＠")) and not after.isspace():
        return False
    return True


def truncate_caption(
    text: str, limit: int | None, weighted: bool = False
) -> str:
    """Cut a caption to fit, on a boundary that does not corrupt it.

    ``weighted`` applies X's counting instead of Python's.
    """
    if not text or limit is None:
        return text or ""

    measure = weighted_length if weighted else len
    if measure(text) <= limit:
        return text

    # Largest cut that fits the measure. Under weighted counting this is well
    # short of `limit` characters.
    fits = min(len(text), limit)
    while fits > 0 and measure(text[:fits]) > limit:
        fits -= 1

    # Then walk back to a boundary that does not corrupt a token - but not at
    # any price. A caption that is one long hashtag has no safe break at all,
    # and giving up the whole string to protect a token nobody can read is
    # worse than the ragged edge.
    floor = int(fits * (1 - MAX_GIVEBACK))
    index = fits
    while index > floor and not _is_safe_break(text, index):
        index -= 1
    if index <= floor:
        index = fits          # no safe break worth taking; accept the hard cut

    trimmed = text[:index].rstrip()
    if trimmed:
        return trimmed
    # Nothing survived the trim. Return the hard fit rather than nothing:
    # exceeding the limit guarantees the platform rejects the post.
    return text[:fits] or text[:1]

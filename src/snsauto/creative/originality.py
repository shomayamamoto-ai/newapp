"""Checking that a generated script is not a competitor's copy.

The script generator is handed the competitor corpus - titles, captions, and
now the telop read out of their videos - and told to learn from it. Nothing in
that arrangement stops it reproducing a phrase verbatim, and the pipeline can
publish without a human reading the result. Posting a rival's exact hook under
a client's brand is a problem that no amount of performance makes up for.

The measure is **longest shared run of characters**, not similarity of meaning.
Two videos about morning routines should share vocabulary; that is the topic,
not a lift. What marks a lift is a long contiguous stretch appearing in both.
In Japanese, which has no spaces, a run of ten or more characters is already
most of a telop card, and generic collocations ("朝食を抜く", "結果はこちら")
are shorter than that - which is why the threshold is a run length rather than
a word-overlap ratio.
"""

from __future__ import annotations

import re
import unicodedata

# A verbatim stretch this long is not coincidence. Calibrated against real
# Japanese phrasing: common collocations run 4-8 characters, so 10 clears them
# while still catching a copied telop card.
RUN_THRESHOLD = 10

# A short line can be wholly generic and still trip a run check, so very short
# generated lines are judged on coverage instead.
SHORT_LINE = 14
COVERAGE_THRESHOLD = 0.75

# Attempts before giving up and flagging rather than looping on the model.
MAX_REGENERATIONS = 2


def normalise(text: str | None) -> str:
    """Compare on content, not on presentation.

    Width, case and punctuation differences are how a copy hides from a naive
    string check, so they are removed before comparing.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s　、。，．!！?？…「」『』（）()\[\]・\-—~〜:：;；]+", "", folded)


def longest_common_run(a: str, b: str) -> str:
    """The longest stretch of characters appearing in both."""
    if not a or not b:
        return ""
    # Rolling comparison over the shorter string's suffixes. The strings here
    # are telop cards and script lines, not documents, so this is cheap.
    best = ""
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > len(best):
                    best = a[i - current[j]: i]
        previous = current
    return best


def check_line(line: str, corpus: list[str]) -> dict | None:
    """Is this generated line lifted from the corpus? None when it is clean."""
    normalised = normalise(line)
    if not normalised:
        return None

    worst = None
    for source in corpus:
        shared = longest_common_run(normalised, normalise(source))
        if not shared:
            continue
        coverage = len(shared) / len(normalised)
        too_long = len(shared) >= RUN_THRESHOLD
        too_much = len(normalised) <= SHORT_LINE and coverage >= COVERAGE_THRESHOLD
        if not (too_long or too_much):
            continue
        if worst is None or len(shared) > len(worst["shared"]):
            worst = {
                "line": line,
                "shared": shared,
                "shared_length": len(shared),
                "coverage": round(coverage, 3),
                "source": source,
            }
    return worst


def script_lines(data: dict) -> list[str]:
    """Every piece of copy a script will actually show or say."""
    out = [data.get("title"), data.get("hook"), data.get("body"), data.get("cta")]
    for line in data.get("lines") or []:
        out.append(line.get("telop"))
        out.append(line.get("narration"))
    return [t for t in out if isinstance(t, str) and t.strip()]


def corpus_from_run(run) -> list[str]:
    """Every piece of competitor copy this script could have lifted.

    Taken from the run rather than from the research summary: the summary
    carries counts and rankings, while the words the generator could copy live
    on the posts themselves and in the telop read out of their videos.

    Single words are deliberately excluded. A shared word is the topic.
    """
    corpus: list[str] = []
    if run is None:
        return corpus
    for post in getattr(run, "posts", []) or []:
        for value in (post.title, post.caption):
            if value and len(value.strip()) > 1:
                corpus.append(value)
        structure = getattr(post, "structure", None)
        onscreen = ((structure.telop if structure else None) or {}).get("onscreen") or {}
        for event in onscreen.get("events") or []:
            text = (event or {}).get("text")
            if text:
                corpus.append(text)
    return corpus


def review(data: dict, corpus: list[str]) -> dict:
    """Every line that looks lifted, worst first."""
    if not corpus:
        return {"checked": False,
                "reason": "比較対象の競合テキストがありません。",
                "findings": []}

    findings = [f for f in (check_line(line, corpus) for line in script_lines(data)) if f]
    findings.sort(key=lambda f: f["shared_length"], reverse=True)
    return {
        "checked": True,
        "clean": not findings,
        "findings": findings,
        "threshold": RUN_THRESHOLD,
    }


def avoid_instruction(findings: list[dict]) -> str:
    """What to tell the model on the retry, naming the exact overlaps."""
    phrases = sorted({f["shared"] for f in findings}, key=len, reverse=True)[:8]
    quoted = "、".join(f"「{p}」" for p in phrases)
    return (
        "\n\n# 重要: 前回の生成は競合投稿の文言と一致しました\n"
        f"次の表現は競合の投稿に含まれています。同じ言い回しを使わず、"
        f"意味が同じでも別の語順・別の語彙で書き直してください: {quoted}\n"
        "構成（尺・フックの型・テンポ）を参考にするのは問題ありませんが、"
        "文言そのものを流用してはいけません。"
    )

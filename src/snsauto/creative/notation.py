"""表記ゆれの統一 - 「アストラ」を「Astra」に、「プレミア」を「Premiere」に。

編集の指示文には必ずといっていいほど表記指定が入る:

    ・表記指定
    「アストラ」は「Astra」と表記
    「プレミア」は「Premiere」と表記

人が毎回直していた作業で、テロップと本文の両方に効く。プロジェクトごとに
辞書を持たせ、台本生成とキャプション生成の両方を通す。

**置換は必ず記録する。** 表記統一には固有の危険があるためで、たとえば
「プレミア」→「Premiere」を入れたまま「プレミアリーグ」を書くと
「Premiereリーグ」になる。長い語を先に当てる、除外語を持てるようにする、
そして**何をどこで置き換えたかを必ず返す**の三つで守っている。最後の一つが
効くのは、辞書の事故は書いた本人しか気づけないため。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# brand_profile に入れる鍵。
NOTATION_KEY = "notation"
EXCEPTION_KEY = "notation_exceptions"


@dataclass(slots=True)
class Replacement:
    source: str
    target: str
    index: int


@dataclass(slots=True)
class NotationResult:
    text: str
    replacements: list[Replacement] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.replacements)

    def summary_ja(self) -> str:
        if not self.replacements:
            return "表記の置換はありません"
        counts: dict[tuple[str, str], int] = {}
        for item in self.replacements:
            counts[(item.source, item.target)] = counts.get((item.source, item.target), 0) + 1
        parts = [f"「{s}」→「{t}」{n}件" for (s, t), n in counts.items()]
        return "、".join(parts)


class NotationRules:
    """ひとつのプロジェクトの表記指定。

    ``exceptions`` に入れた語は、その中に置換対象が含まれていても触らない。
    「プレミアリーグ」を守るのがこれ。
    """

    def __init__(self, mapping: dict | None = None, exceptions=None):
        self.mapping = {
            str(k): str(v) for k, v in (mapping or {}).items()
            if str(k).strip() and str(v).strip() and str(k) != str(v)
        }
        self.exceptions = [str(e) for e in (exceptions or []) if str(e).strip()]

    @classmethod
    def from_project(cls, project) -> NotationRules:
        profile = getattr(project, "brand_profile", None) or {}
        return cls(profile.get(NOTATION_KEY), profile.get(EXCEPTION_KEY))

    def __bool__(self) -> bool:
        return bool(self.mapping)

    def _protected_spans(self, text: str) -> list[tuple[int, int]]:
        spans = []
        for word in self.exceptions:
            for match in re.finditer(re.escape(word), text):
                spans.append(match.span())
        return spans

    def apply(self, text: str | None) -> NotationResult:
        if not text or not self.mapping:
            return NotationResult(text=text or "")

        protected = self._protected_spans(text)
        # 長い語から当てる。「プレミアプロ」を定義していれば「プレミア」より
        # 先に一致させないと、後半だけ残って壊れる。
        sources = sorted(self.mapping, key=len, reverse=True)

        out: list[str] = []
        replacements: list[Replacement] = []
        cursor = 0
        while cursor < len(text):
            for source in sources:
                if not text.startswith(source, cursor):
                    continue
                end = cursor + len(source)
                if any(a <= cursor and end <= b for a, b in protected):
                    continue                      # 除外語の内側なので触らない
                target = self.mapping[source]
                out.append(target)
                replacements.append(Replacement(source, target, cursor))
                cursor = end
                break
            else:
                out.append(text[cursor])
                cursor += 1
        return NotationResult(text="".join(out), replacements=replacements)

    def apply_to_lines(self, lines: list[dict], fields=("telop", "narration")) -> list[Replacement]:
        """台本の各行に当てる。行は破壊的に書き換える。"""
        found: list[Replacement] = []
        for line in lines:
            for name in fields:
                value = line.get(name)
                if not isinstance(value, str) or not value:
                    continue
                result = self.apply(value)
                if result.changed:
                    line[name] = result.text
                    found.extend(result.replacements)
        return found

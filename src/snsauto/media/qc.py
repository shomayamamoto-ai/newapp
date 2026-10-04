"""Inspecting a finished video before it goes out.

The pipeline renders and publishes. Nothing between those two steps looks at
the result, so a video whose first telop lands at four seconds, or whose text
flashes faster than anyone reads, is published exactly as confidently as a
good one - and the only feedback is the retention curve a day later, by which
time the post is spent.

**The thresholds are not invented.** Almost every check is calibrated against
two things this tool already measures:

* the competitor corpus for the keyword - telop density, cut pace and duration
  of the posts that are actually working in this niche right now;
* the account's own retention data - where its viewers have historically left.

Where neither exists the check says so and stays quiet, rather than asserting
a number somebody made up. A quality gate that fails a good video on a guessed
threshold gets switched off, and then it protects nothing.

Checks are advisory by default and blocking only where the defect is certain
and visible - text that leaves the safe area is covered by the platform's own
UI on every device, which is not a matter of taste.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Platform
from .ffmpeg import FFmpegError, probe
import re

from .subtitles import PLATFORM_TELOP_STYLES, TelopStyle, wrap_text

URL_RE = re.compile(r"https?://\S+")

BLOCK, WARN, INFO = "block", "warn", "info"

# Platforms normalise loudness to roughly this, so a master far from it gets
# moved - and the relative dynamics of the mix move with it.
TARGET_LUFS = -14.0
LUFS_TOLERANCE = 3.0

# Without a corpus to compare against, only defects that are certain get
# reported. These two are: a viewer cannot read text that is not on screen,
# and cannot see text the platform's UI is covering.
DEFAULT_FIRST_TELOP_SEC = 2.0

# The ASS canvas the subtitle styles are authored against, and the room
# left clear at the top for each platform's own header.
SAFE_PLAY_HEIGHT = 1920
SAFE_TOP_MARGIN = 220


@dataclass
class Issue:
    check: str
    severity: str
    what: str
    why: str
    fix: str
    at_sec: float | None = None

    def as_dict(self) -> dict:
        return {
            "check": self.check, "severity": self.severity, "what": self.what,
            "why": self.why, "fix": self.fix, "at_sec": self.at_sec,
        }


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    measured: dict = field(default_factory=dict)
    calibration: dict = field(default_factory=dict)

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == BLOCK]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == WARN]

    @property
    def passed(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "blocking": len(self.blocking),
            "warnings": len(self.warnings),
            "issues": [i.as_dict() for i in self.issues],
            "measured": self.measured,
            "calibration": self.calibration,
        }


def _corpus_targets(research: dict | None) -> dict:
    """What the posts that are working in this niche actually look like."""
    if not research or not research.get("count"):
        return {}
    duration = research.get("duration_sec") or {}
    spread = duration.get("spread_top") or {}
    return {
        "source": f"競合上位{research.get('count')}件",
        "duration_p25": spread.get("p25"),
        "duration_p75": spread.get("p75"),
        "duration_band": duration.get("band_top"),
    }


def check_render(
    render,
    shots,
    platform: Platform,
    research: dict | None = None,
    retention: dict | None = None,
) -> Report:
    """Everything worth catching before a video is public."""
    report = Report(calibration=_corpus_targets(research))
    shots = sorted(shots or [], key=lambda s: s.index)
    style = PLATFORM_TELOP_STYLES.get(platform, TelopStyle())

    _check_file(render, platform, report)
    _check_hook(shots, retention, report)
    _check_readability(shots, research, report)
    _check_safe_area(shots, style, platform, report)
    _check_pacing(shots, research, report)
    _check_duration(render, research, report)

    # Severity first, then time. One weak shot can raise three issues, and the
    # one that has to be fixed before publishing should not be third in a list.
    order = {BLOCK: 0, WARN: 1, INFO: 2}
    report.issues.sort(key=lambda i: (order[i.severity], i.at_sec or 0.0))
    return report


# ---------------------------------------------------------------- checks


def _check_file(render, platform: Platform, report: Report) -> None:
    from .assemble import PLATFORM_SPECS

    path = getattr(render, "path", None)
    if not path:
        report.issues.append(Issue(
            "file", BLOCK, "動画ファイルがありません", "投稿できません",
            "レンダリングをやり直してください",
        ))
        return
    try:
        info = probe(path)
    except (FFmpegError, OSError) as exc:
        report.issues.append(Issue(
            "file", BLOCK, f"動画を読み取れません: {exc}",
            "壊れたファイルは投稿時に拒否されます", "レンダリングをやり直してください",
        ))
        return

    report.measured.update({
        "duration_sec": info.get("duration"),
        "resolution": f"{info.get('width')}x{info.get('height')}",
        "has_audio": info.get("has_audio"),
    })

    # probe() reports zeros for a file it cannot decode rather than raising,
    # so a truncated or corrupt render reaches here looking merely empty. A
    # video with no duration or no picture is not a video.
    if not info.get("duration") or not info.get("width") or not info.get("height"):
        report.issues.append(Issue(
            "file", BLOCK, "動画の長さまたは解像度が0です",
            "書き出しが途中で失敗したか、ファイルが壊れています。"
            "このまま投稿するとアップロードで失敗します",
            "レンダリングをやり直してください",
        ))
        return

    spec = PLATFORM_SPECS.get(platform)
    if spec and info.get("width") and info.get("height"):
        expected = round(spec.width / spec.height, 3)
        actual = round(info["width"] / info["height"], 3)
        if abs(expected - actual) > 0.02:
            report.issues.append(Issue(
                "aspect", BLOCK,
                f"アスペクト比が {info['width']}x{info['height']} です",
                f"{platform.value.upper()} は {spec.width}x{spec.height} 想定で、"
                "このままだと自動的に切り取られるか余白が入ります",
                f"{spec.width}x{spec.height} で書き出し直してください",
            ))

    if not info.get("has_audio"):
        report.issues.append(Issue(
            "audio", WARN, "音声トラックがありません",
            "ショート動画は音声の有無で配信が変わります",
            "ナレーションかBGMを入れてください",
        ))
        return

    _check_loudness(path, report)


def _check_loudness(path, report: Report) -> None:
    from ..research.audio import analyze_audio

    try:
        audio = analyze_audio(path)
    except (FFmpegError, OSError):
        return
    lufs = audio.get("loudness_lufs")
    report.measured["loudness_lufs"] = lufs
    if lufs is None:
        return
    if abs(lufs - TARGET_LUFS) > LUFS_TOLERANCE:
        direction = "大きすぎ" if lufs > TARGET_LUFS else "小さすぎ"
        report.issues.append(Issue(
            "loudness", WARN, f"音量が {lufs:.1f} LUFS で{direction}ます",
            f"各プラットフォームは約{TARGET_LUFS:.0f} LUFSに正規化するため、"
            "そのままだと意図した音のバランスから動きます",
            f"{TARGET_LUFS:.0f} LUFS 付近に調整してください",
        ))


def _check_hook(shots, retention: dict | None, report: Report) -> None:
    """How fast the first telop lands.

    Calibrated on the account's own retention when there is any: if its
    viewers historically leave at 2 seconds, a hook arriving at 2.5 is late
    for *these* viewers, whatever a general guideline says.
    """
    if not shots:
        return
    first = next((s for s in shots if (s.telop or "").strip()), None)
    if first is None:
        report.issues.append(Issue(
            "hook", WARN, "テロップが1枚もありません",
            "無音で見る視聴者には内容が伝わりません",
            "少なくとも冒頭にテロップを入れてください",
        ))
        return

    report.measured["first_telop_sec"] = round(first.start, 2)
    limit = DEFAULT_FIRST_TELOP_SEC
    basis = "短尺動画の一般的な目安"
    if retention and retention.get("usable"):
        drops = retention.get("recurring_drops") or []
        if drops:
            earliest = min(d["second"] for d in drops)
            limit = min(limit, max(0.5, earliest - 0.5))
            basis = f"このアカウントの視聴者は{earliest}秒で離脱しがち"
            report.calibration["hook_basis"] = basis

    if first.start > limit:
        report.issues.append(Issue(
            "hook", WARN,
            f"最初のテロップが {first.start:.1f}秒 で、{limit:.1f}秒 より遅いです",
            f"{basis}のため、読む前に離脱される可能性があります",
            "1枚目を前倒しするか、冒頭に短いテロップを足してください",
            at_sec=first.start,
        ))


def _check_readability(shots, research: dict | None, report: Report) -> None:
    """Whether the text can be read in the time it is on screen.

    The ceiling comes from the corpus: if the competing posts that are working
    show 4 characters a second, showing 11 is not a bold choice, it is text
    nobody in this niche is reading.
    """
    if not shots:
        return
    rates = []
    for shot in shots:
        text = (shot.telop or "").replace("\n", "")
        hold = max(0.01, shot.end - shot.start)
        if text:
            rates.append(len(text) / hold)
    if rates:
        report.measured["chars_per_sec_max"] = round(max(rates), 2)

    ceiling = None
    card_ceiling = None
    onscreen = ((research or {}).get("telop") or {})
    corpus_rate = onscreen.get("chars_per_sec")
    corpus_chars = onscreen.get("avg_chars")
    if corpus_chars:
        # Twice what is working in the niche is where a card stops being a
        # telop and becomes a paragraph.
        card_ceiling = corpus_chars * 2
        report.calibration["avg_chars_corpus"] = corpus_chars
    if corpus_rate:
        # Half again above what is working is the point where it stops being a
        # stylistic difference.
        ceiling = corpus_rate * 1.5
        report.calibration["chars_per_sec_corpus"] = corpus_rate

    for shot in shots:
        text = (shot.telop or "").replace("\n", "")
        hold = max(0.01, shot.end - shot.start)
        if not text:
            continue
        rate = len(text) / hold
        if ceiling and rate > ceiling:
            report.issues.append(Issue(
                "readability", WARN,
                f"{shot.start:.1f}秒: {len(text)}字を{hold:.1f}秒しか表示していません"
                f"（毎秒{rate:.1f}字）",
                f"この調査の競合上位は毎秒{corpus_rate:.1f}字です。"
                "読み切れない可能性があります",
                "テロップを短くするか、表示時間を伸ばしてください",
                at_sec=shot.start,
            ))
        elif card_ceiling and len(text) > card_ceiling:
            report.issues.append(Issue(
                "readability", WARN,
                f"{shot.start:.1f}秒: 1枚のテロップが{len(text)}字あります",
                f"この調査の競合上位は1枚あたり{corpus_chars:.0f}字です。"
                "一度に出す量が多いと読み飛ばされます",
                "2枚に分けてください", at_sec=shot.start,
            ))
        elif hold < 0.8 and len(text) > 6:
            # No corpus to compare against, but under 0.8 seconds nothing of
            # this length is read by anyone.
            report.issues.append(Issue(
                "readability", WARN,
                f"{shot.start:.1f}秒: {len(text)}字の表示が{hold:.1f}秒です",
                "この長さの文字を読むには短すぎます",
                "表示時間を伸ばしてください", at_sec=shot.start,
            ))


def _check_safe_area(shots, style: TelopStyle, platform: Platform, report: Report) -> None:
    """Text that will not sit where the layout assumes it does.

    Horizontal fit is correct by construction for videos we render - the
    wrapper enforces the platform's safe width. Two things still escape it,
    and both are visible on every device rather than matters of taste:

    * a token that cannot be broken (a URL, a long hashtag, an English word)
      is emitted whole and runs past the margin into the platform's UI;
    * enough wrapped lines to grow the block past the vertical safe band, so
      the top of the telop climbs into the header.
    """
    # ASS line height is roughly 1.2x the font size, and the block is laid out
    # upward from margin_v.
    line_height = style.size * 1.2
    available = SAFE_PLAY_HEIGHT - style.margin_v - SAFE_TOP_MARGIN
    max_lines = max(1, int(available // line_height))
    report.calibration["max_telop_lines"] = max_lines

    for shot in shots:
        text = (shot.telop or "").strip()
        if not text:
            continue
        lines = wrap_text(text, style.max_width)

        # The wrapper breaks anywhere, which is correct for Japanese and wrong
        # for a URL: split across four lines it can be neither read nor typed.
        split_url = _url_broken_across_lines(text, lines)
        if split_url:
            report.issues.append(Issue(
                "safe_area", BLOCK,
                f"{shot.start:.1f}秒: URL「{split_url[:36]}…」が複数行に分断されています",
                "日本語は任意の位置で改行できるためURLも途中で折られます。"
                "分断されたURLは読むことも入力することもできません",
                "URLは短縮するか、テロップから外してキャプションに置いてください",
                at_sec=shot.start,
            ))
            continue

        if len(lines) > max_lines:
            report.issues.append(Issue(
                "safe_area", BLOCK,
                f"{shot.start:.1f}秒: テロップが{len(lines)}行になります"
                f"（{platform.value.upper()} の安全域は{max_lines}行）",
                "行数が多いと上端が画面外やヘッダーに掛かります",
                f"1枚を{max_lines}行以内に収めるか、カットを分けてください",
                at_sec=shot.start,
            ))


def _url_broken_across_lines(text: str, lines: list[str]) -> str | None:
    """A URL in the source that no single wrapped line contains whole."""
    for match in URL_RE.finditer(text):
        url = match.group(0)
        if not any(url in line for line in lines):
            return url
    return None


def _check_pacing(shots, research: dict | None, report: Report) -> None:
    """Shots held far longer than what is working in this niche."""
    if not shots:
        return
    holds = [s.end - s.start for s in shots if s.end > s.start]
    if not holds:
        return
    report.measured["longest_shot_sec"] = round(max(holds), 2)

    pacing = ((research or {}).get("pacing") or {})
    corpus_avg = pacing.get("avg_shot_sec")
    if not corpus_avg:
        return
    report.calibration["avg_shot_sec_corpus"] = corpus_avg
    ceiling = corpus_avg * 2.5

    for shot in shots:
        hold = shot.end - shot.start
        if hold > ceiling:
            report.issues.append(Issue(
                "pacing", WARN,
                f"{shot.start:.1f}秒: 同じ画が{hold:.1f}秒続きます",
                f"この調査の競合上位は平均{corpus_avg:.1f}秒でカットしています",
                "カットを割るか、動きを足してください", at_sec=shot.start,
            ))


def _check_duration(render, research: dict | None, report: Report) -> None:
    """Length against the band the winning posts occupy."""
    duration = getattr(render, "duration_sec", None)
    if not duration:
        return
    targets = _corpus_targets(research)
    low, high = targets.get("duration_p25"), targets.get("duration_p75")
    if not (low and high):
        return
    if duration < low * 0.6 or duration > high * 1.6:
        report.issues.append(Issue(
            "duration", INFO,
            f"尺が{duration:.0f}秒です",
            f"この調査の上位投稿は{low:.0f}〜{high:.0f}秒に集まっています",
            "その範囲に寄せると、同じ枠で比較されやすくなります",
        ))


def summary(report: Report) -> str:
    """One line for a log or a job result."""
    if report.passed and not report.warnings:
        return "品質チェック: 問題なし"
    parts = []
    if report.blocking:
        parts.append(f"要修正 {len(report.blocking)}件")
    if report.warnings:
        parts.append(f"警告 {len(report.warnings)}件")
    return "品質チェック: " + "、".join(parts)

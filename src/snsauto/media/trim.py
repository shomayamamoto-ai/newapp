"""無音カットと言い直し検出 - 撮影した素材を、言葉を欠かずに詰める。

ここまでの映像機能は絵コンテから動画を生成するものだけで、**撮影済みの
talking head 素材を編集する経路が無かった**。運用代行で毎日発生するのは
そちらなので、この欠落を埋める。

仕様は編集者が実務で使っている指示文をそのまま要件にしている:

    無音と言い直しをカットして、テンポよく編集してください。
    ただし、単語の途中はカットせず、「ッ」などの促音、子音、語頭・語尾を
    必ず残してください。
    無音カットは、文やフレーズの間にある不要な間だけを対象にしてください。
    目安は0.3秒以上とし、発話の前後には0.1秒程度の余白を残してください。
    音量だけで機械的に判断せず、言葉が欠ける可能性のある箇所は残して
    ください。テンポよりも、自然で明瞭な発音を優先してください。

この最後の一文がこのモジュールの設計方針そのものになっている。``silencedetect``
は**候補を出すだけ**で、採否はこちらで決める。閾値をそのまま採用すれば
「音量だけで機械的に判断」したことになるため、検出は本来より細かく走らせて
(``DETECT_MIN_SEC``)、短い間や子音のエネルギーがある箇所をこちら側で
明示的に残す。

**測定して決めた定数** (tests/test_trim.py が同じ合成音声で固定している):

    区間            speech帯(300-3400Hz)  高域(4-8kHz)
    母音            -27.6 dB              -72.8 dB
    促音「ッ」0.12s  -82.2 dB              -87.6 dB
    文間の間 0.70s   -88.6 dB              -89.1 dB

促音と文間の間は speech 帯では区別がつかない(-82 と -88)。**長さでしか
分けられない**ため 0.3 秒を境界に使う。

無声子音(/s/ /ʃ/ /h/)の保護には高域を使うが、二つ測り直して形を決めた。

1. 区間全体のピークでは駄目だった。発話の立ち上がりを拾ってしまい、
   切るべき「真の間」が最大値(床比 33dB)になって逆転する。**実際に削除
   される内側**(前後の余白を除いた部分)だけを測る。
2. 高域の絶対値でも駄目だった。母音時の高域(-72dB)と小さい摩擦音(-69dB)が
   近すぎる。録音レベルは撮影者ごとに違うので、**そのファイル自身の高域床**
   (全窓の5パーセンタイル)との差で見る。室内ノイズは低域中心で高域には
   ほとんど乗らないため、この床は実録音でも安定する。

    ケース                内側高域   床との差   判定
    真の間 0.70s          -90.0 dB     0.0 dB   切る
    真の間 0.60s          -90.0 dB     0.0 dB   切る
    /s/ が続く 0.70s      -61.4 dB    28.6 dB   残す
    小さい /s/            -57.2 dB    15.8 dB   残す
    さらに小さい /s/      -63.7 dB     9.3 dB   残す
    ノイズ床すれすれの /s/ -69.6 dB     3.4 dB   ★分離できない

境界は 8dB に置いた。★の一件は保護できない。ノイズ床から 3.4dB しか
出ておらず、無音と区別する材料が無いため。**この水準の子音を守っているのは
高域判定ではなく前後 0.1 秒の余白**で、語頭・語尾はそちらで残る。
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg import FFmpegError, ffmpeg_path, probe, run_ffmpeg

log = logging.getLogger(__name__)

# 指示文の「目安は0.3秒以上」。これより短い間は文の切れ目ではなく、促音や
# 破裂音の閉鎖区間である可能性が高いので候補にもしない。
MIN_GAP_SEC = 0.30

# 指示文の「発話の前後には0.1秒程度の余白」。カットは間の中央だけを対象に
# し、両端にこの余白を必ず残す。語頭・語尾が削れるのはここを省いたとき。
HANDLE_SEC = 0.10

# ffmpeg に出させる最小長。MIN_GAP_SEC より十分短くしてあるのは意図的で、
# 「短いから候補にしなかった」という判断を記録に残すため。ffmpeg の閾値を
# そのまま方針にすると、何を見送ったのかが分からなくなる。
DETECT_MIN_SEC = 0.08

# 無音とみなす音量。実際の採否はこの後の判定で決まるので、これは
# 「候補に挙げる」ためのゆるい線。
NOISE_FLOOR_DB = -35.0

# 音声の帯域と、無声子音が乗る高域。
SPEECH_BAND = (300, 3400)
FRICATIVE_BAND = (4000, 8000)

# 削除する区間の高域が、そのファイル自身の高域床からこれ以上出ていれば、
# 無声子音(/s/ /ʃ/ /h/ /ts/)が鳴っているとみなして切らない。上の表のとおり
# 切るべき間は 0.0dB、守るべき子音は 9.3dB 以上に出る。
CONSONANT_MARGIN_DB = 8.0

# 高域床を取るパーセンタイル。室内ノイズを拾い、たまたま鳴っている子音に
# 引きずられない位置。
FLOOR_PERCENTILE = 0.05

FLOOR_DB = -90.0
WINDOW_SEC = 0.020

_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")
_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.]+|-?inf|nan)")

KEEP, CUT = "keep", "cut"


@dataclass(slots=True)
class Gap:
    """ひとつの無音候補と、それをどう扱ったか。

    ``reason`` は残した理由を日本語で持つ。「なぜ詰まっていないのか」を
    編集者が確認できないと、この種の自動化は信用されない。
    """

    start: float
    end: float
    decision: str
    reason: str
    cut_start: float | None = None
    cut_end: float | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def removed(self) -> float:
        if self.cut_start is None or self.cut_end is None:
            return 0.0
        return max(0.0, self.cut_end - self.cut_start)


@dataclass(slots=True)
class TrimPlan:
    """切る前の計画。適用しなくても、そのまま確認用の資料になる。"""

    source: str
    duration: float
    gaps: list[Gap] = field(default_factory=list)
    keep: list[tuple[float, float]] = field(default_factory=list)

    @property
    def removed_sec(self) -> float:
        return sum(gap.removed for gap in self.gaps)

    @property
    def result_sec(self) -> float:
        return max(0.0, self.duration - self.removed_sec)

    @property
    def cuts(self) -> list[Gap]:
        return [gap for gap in self.gaps if gap.decision == CUT]

    @property
    def kept(self) -> list[Gap]:
        return [gap for gap in self.gaps if gap.decision == KEEP]

    def summary(self) -> dict:
        return {
            "source": self.source,
            "duration_sec": round(self.duration, 3),
            "result_sec": round(self.result_sec, 3),
            "removed_sec": round(self.removed_sec, 3),
            "cut_count": len(self.cuts),
            "kept_count": len(self.kept),
            "segments": len(self.keep),
        }

    def report_ja(self) -> str:
        """編集者が読んで確認するための一枚。"""
        lines = [
            f"{self.duration:.2f}秒 → {self.result_sec:.2f}秒"
            f"（{self.removed_sec:.2f}秒を削除、{len(self.cuts)}箇所）",
        ]
        if self.kept:
            lines.append("")
            lines.append("残した間（言葉が欠ける可能性があるため）:")
            for gap in self.kept:
                lines.append(
                    f"  {gap.start:6.2f}s  {gap.duration:.2f}秒  {gap.reason}"
                )
        return "\n".join(lines)


def _run_stderr(args: list[str]) -> str:
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", *args],
        capture_output=True, text=True,
    )
    return proc.stderr


def detect_gaps(path: str | Path) -> list[tuple[float, float]]:
    """ffmpeg が提案する無音区間。採否はここでは決めない。"""
    stderr = _run_stderr([
        "-i", str(path), "-af",
        f"silencedetect=noise={NOISE_FLOOR_DB}dB:d={DETECT_MIN_SEC}",
        "-f", "null", "-",
    ])
    starts = [float(v) for v in _SILENCE_START.findall(stderr)]
    ends = [float(v) for v in _SILENCE_END.findall(stderr)]
    # 末尾の無音は silence_end を伴わないことがある。組めた分だけ使う。
    return [(max(0.0, s), e) for s, e in zip(starts, ends) if e > s]


def band_series(path: str | Path, band: tuple[int, int]) -> list[float]:
    """20ms窓ごとの帯域RMS(dB)。無音窓は FLOOR_DB。"""
    low, high = band
    chain = (
        "aresample=48000,aformat=channel_layouts=mono,"
        f"highpass=f={low},lowpass=f={high},"
        f"asetnsamples=n={int(48000 * WINDOW_SEC)}:p=0,"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error",
         "-i", str(path), "-af", chain, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    values = []
    for raw in _RMS.findall(proc.stdout):
        try:
            values.append(max(FLOOR_DB, float(raw)))
        except ValueError:
            values.append(FLOOR_DB)          # -inf / nan
    return values


def _interior_peak(
    series: list[float], start: float, end: float, handle: float
) -> float:
    """実際に削除される内側だけのピーク。

    区間全体で測ると発話の立ち上がりを拾い、切るべき間ほど高い値が出て
    判定が逆転する。守りたいのは「消す部分に音が入っていないか」なので、
    残す余白を除いた範囲だけを見る。
    """
    inner_start, inner_end = start + handle, end - handle
    if inner_end - inner_start <= 0:
        return FLOOR_DB
    # 境界の窓は隣の発話と混ざるため、内側に1窓ぶん寄せる。
    first = int(inner_start / WINDOW_SEC) + 1
    last = max(first + 1, int(inner_end / WINDOW_SEC))
    window = series[first:last]
    return max(window) if window else FLOOR_DB


def _floor_db(series: list[float]) -> float:
    """そのファイルの高域床。録音レベルの違いを吸収する基準。"""
    if not series:
        return FLOOR_DB
    ordered = sorted(series)
    return ordered[max(0, int(len(ordered) * FLOOR_PERCENTILE))]


def plan_trim(
    path: str | Path,
    min_gap: float = MIN_GAP_SEC,
    handle: float = HANDLE_SEC,
    keep_head_tail: bool = True,
) -> TrimPlan:
    """どこを詰めるかを決める。ファイルは書き換えない。

    ``keep_head_tail`` を立てたままにすると、素材の先頭と末尾の無音は
    候補から外す。ここは「文やフレーズの間」ではなく、撮影の頭と尻であって、
    詰めるかどうかは編集方針の話だから。
    """
    info = probe(path)
    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        raise FFmpegError(f"尺を取得できません: {path}")
    if not info.get("has_audio"):
        raise FFmpegError(
            f"音声トラックがありません: {path}。無音カットは音声を基準に"
            "判断するため、音声なしの素材には適用できません。"
        )

    plan = TrimPlan(source=str(path), duration=duration)
    fricatives = band_series(path, FRICATIVE_BAND)
    floor = _floor_db(fricatives)

    for start, end in detect_gaps(path):
        end = min(end, duration)
        gap = Gap(start=start, end=end, decision=KEEP, reason="")

        if keep_head_tail and start <= 0.01:
            gap.reason = "素材の先頭（文の間ではありません）"
        elif keep_head_tail and end >= duration - 0.01:
            gap.reason = "素材の末尾（文の間ではありません）"
        elif gap.duration < min_gap:
            gap.reason = f"{min_gap:.1f}秒未満の間（促音・子音の可能性）"
        elif (
            _interior_peak(fricatives, start, end, handle) - floor
            > CONSONANT_MARGIN_DB
        ):
            # 「音量だけで機械的に判断せず」。speech帯では無音でも、消そうと
            # している部分の高域が床から出ていれば無声子音が続いている。
            above = _interior_peak(fricatives, start, end, handle) - floor
            gap.reason = f"無声子音のエネルギーを検出（高域が床より{above:.0f}dB上）"
        else:
            cut_start = start + handle
            cut_end = end - handle
            if cut_end - cut_start <= 0:
                gap.reason = f"前後に{handle:.1f}秒の余白を残すと切る幅が残らない"
            else:
                gap.decision = CUT
                gap.reason = f"文の間（{gap.duration:.2f}秒）"
                gap.cut_start = cut_start
                gap.cut_end = cut_end
        plan.gaps.append(gap)

    plan.keep = _keep_segments(duration, plan.cuts)
    return plan


def _keep_segments(duration: float, cuts: list[Gap]) -> list[tuple[float, float]]:
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for gap in sorted(cuts, key=lambda g: g.cut_start or 0.0):
        if gap.cut_start is None or gap.cut_end is None:
            continue
        if gap.cut_start > cursor:
            segments.append((cursor, gap.cut_start))
        cursor = max(cursor, gap.cut_end)
    if cursor < duration:
        segments.append((cursor, duration))
    return [(s, e) for s, e in segments if e - s > 0.001]


def _concat_graph(keep: list[tuple[float, float]]) -> str:
    """残す区間を切り出して連結するフィルタグラフ。

    区間ごとに ``trim``/``atrim`` で切り、``PTS-STARTPTS`` で各区間の時刻を
    0 から振り直してから ``concat`` する。フレームの**実際の時刻**を使うので、
    可変フレームレートの素材でも映像と音声が揃う。

    以前は ``select`` + ``setpts=N/FRAME_RATE/TB`` だった。これはフレームが
    等間隔に並んでいる前提で時刻を作り直すため、スマホで撮った可変フレーム
    レートの素材では、5.6秒の素材で映像 2.72秒・音声 4.65秒と 1.9秒ずれた。
    talking head では口と声が合わなくなる。
    """
    parts, labels = [], []
    for index, (start, end) in enumerate(keep):
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]"
        )
        parts.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
        )
        labels.append(f"[v{index}][a{index}]")
    parts.append(f"{''.join(labels)}concat=n={len(keep)}:v=1:a=1[v][a]")
    return ";".join(parts)


def apply_trim(plan: TrimPlan, destination: str | Path) -> Path:
    """計画どおりに詰めた動画を書き出す。中間ファイルは作らない。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if not plan.cuts:
        # 切る箇所が無いなら再エンコードしない。品質を落とす理由がない。
        run_ffmpeg(["-y", "-i", plan.source, "-c", "copy", str(destination)])
        return destination

    run_ffmpeg([
        "-y", "-i", plan.source,
        "-filter_complex", _concat_graph(plan.keep),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        # 先頭の moov で、アップロード先がダウンロード完了を待たずに読める。
        "-movflags", "+faststart",
        str(destination),
    ])
    return destination


# ---------- 言い直し ----------

# これより短い発話のかたまりは、言い直し（言いかけて止めた）の可能性がある。
# 実測ではなく構造的な目安で、下の docstring のとおり判定には使わない。
SHORT_TAKE_SEC = 1.2

# 直後の発話がこの倍率以上に長ければ、前者が「言いかけ」に見える。
RESTART_RATIO = 1.8


@dataclass(slots=True)
class RetakeCandidate:
    start: float
    end: float
    reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def speech_segments(plan: TrimPlan) -> list[tuple[float, float]]:
    """無音候補の外側 = 発話のかたまり。ここは音響だけで確実に出る。"""
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for gap in sorted(plan.gaps, key=lambda g: g.start):
        if gap.start > cursor:
            segments.append((cursor, gap.start))
        cursor = max(cursor, gap.end)
    if cursor < plan.duration:
        segments.append((cursor, plan.duration))
    return [(s, e) for s, e in segments if e - s > 0.05]


def retake_candidates(plan: TrimPlan) -> list[RetakeCandidate]:
    """言い直しの**候補**。カットはしない。

    指示文は「無音と言い直しをカットして」だが、言い直しの判定には
    「同じことをもう一度言った」という**言語の情報**が要る。このツールには
    文字起こしが無く、音響だけで「言い直し」と「短い一言」を分ける材料は
    無い。無理に判定すれば、同じ指示文の中にある「言葉が欠ける可能性のある
    箇所は残してください」に正面から反する。

    そこで、切らずに**候補として出すだけ**にしている。短い発話の直後に
    はっきり長い発話が続く箇所は言いかけの形をしているので、編集者が
    その時刻に飛んで確認できる。時間の節約にはなり、誤って言葉を消すことは
    ない。文字起こしを入れれば、ここは判定に変えられる。
    """
    segments = speech_segments(plan)
    found: list[RetakeCandidate] = []
    for index, (start, end) in enumerate(segments[:-1]):
        duration = end - start
        following = segments[index + 1]
        next_duration = following[1] - following[0]
        if duration <= SHORT_TAKE_SEC and next_duration >= duration * RESTART_RATIO:
            found.append(RetakeCandidate(
                start=start, end=end,
                reason=(
                    f"{duration:.2f}秒の短い発話の直後に{next_duration:.2f}秒の"
                    "発話が続いています。言いかけの可能性があります"
                ),
            ))
    return found

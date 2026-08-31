"""One-touch video assembly: stills + telop + BGM -> a platform-ready file.

Every clip is normalised to an identical codec/resolution/rate profile before
concatenation. That is the difference between a concat that works and one that
silently drops audio or stutters: the concat demuxer requires matching streams,
and generated stills have no audio track at all, so a silent track is
synthesised for each.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..models import Platform
from .ffmpeg import FFmpegError, probe, run_ffmpeg
from .subtitles import PLATFORM_TELOP_STYLES, TelopCue, TelopStyle, build_ass


@dataclass(slots=True)
class VideoSpec:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    video_bitrate: str = "6M"
    audio_bitrate: str = "128k"
    max_duration_sec: float | None = None

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"


PLATFORM_SPECS: dict[Platform, VideoSpec] = {
    Platform.TIKTOK: VideoSpec(1080, 1920, 30, max_duration_sec=600),
    Platform.INSTAGRAM: VideoSpec(1080, 1920, 30, max_duration_sec=90),
    Platform.YOUTUBE: VideoSpec(1080, 1920, 30, "8M", max_duration_sec=180),
    Platform.X: VideoSpec(1080, 1920, 30, "5M", max_duration_sec=140),
}


def _scale_pad(spec: VideoSpec) -> str:
    """Fit any source into the target frame without distortion or cropping."""
    return (
        f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=decrease,"
        f"pad={spec.width}:{spec.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={spec.fps},format=yuv420p"
    )


def still_to_clip(
    image_path: str | Path,
    out_path: str | Path,
    duration: float,
    spec: VideoSpec,
    ken_burns: bool = True,
) -> Path:
    """Turn one still into a clip, optionally with a slow push-in.

    A static still reads as dead air on short-form feeds; the push-in is what
    makes a generated-image sequence look edited rather than slideshow-like.
    """
    duration = max(0.4, float(duration))
    out_path = Path(out_path)
    frames = max(1, int(round(duration * spec.fps)))

    if ken_burns:
        # Oversample first: zoompan quantises its offsets to integers, and
        # zooming a frame-sized input produces visible judder.
        vf = (
            f"scale={spec.width * 2}:{spec.height * 2}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={spec.width * 2}:{spec.height * 2},"
            f"zoompan=z='min(1+0.0012*on,1.12)':d={frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={spec.size}:fps={spec.fps},"
            f"setsar=1,format=yuv420p"
        )
    else:
        vf = _scale_pad(spec)

    run_ffmpeg([
        "-y", "-loop", "1", "-t", f"{duration:.3f}", "-i", str(image_path),
        "-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", spec.video_bitrate,
        "-pix_fmt", "yuv420p", "-r", str(spec.fps),
        "-c:a", "aac", "-b:a", spec.audio_bitrate, "-ar", "44100", "-ac", "2",
        "-shortest", str(out_path),
    ])
    return out_path


def normalize_clip(
    src: str | Path, out_path: str | Path, spec: VideoSpec, duration: float | None = None
) -> Path:
    """Re-encode existing footage to the target profile, adding silence if mute."""
    info = probe(src)
    args = ["-y", "-i", str(src)]
    if not info["has_audio"]:
        args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest"]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    args += [
        "-vf", _scale_pad(spec),
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", spec.video_bitrate,
        "-pix_fmt", "yuv420p", "-r", str(spec.fps),
        "-c:a", "aac", "-b:a", spec.audio_bitrate, "-ar", "44100", "-ac", "2",
        str(out_path),
    ]
    run_ffmpeg(args)
    return Path(out_path)


def concat_clips(clips: list[Path], out_path: str | Path, workdir: Path) -> Path:
    """Concat pre-normalised clips. Single-clip input is just a copy."""
    if not clips:
        raise FFmpegError("nothing to concatenate")
    out_path = Path(out_path)
    if len(clips) == 1:
        shutil.copy(clips[0], out_path)
        return out_path

    listing = workdir / "concat.txt"
    listing.write_text(
        "".join(f"file '{c.resolve().as_posix()}'\n" for c in clips), encoding="utf-8"
    )
    run_ffmpeg([
        "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", str(out_path),
    ])
    return out_path


def burn_telop(
    video: str | Path, ass_path: str | Path, out_path: str | Path, spec: VideoSpec
) -> Path:
    # ffmpeg parses the filter string, so ':' and '\' inside the path must be
    # escaped or the filename is read as another filter option.
    escaped = str(Path(ass_path).resolve()).replace("\\", "/").replace(":", "\\:")
    run_ffmpeg([
        "-y", "-i", str(video),
        "-vf", f"subtitles='{escaped}'",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", spec.video_bitrate,
        "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_path),
    ])
    return Path(out_path)


def mix_audio(
    video: str | Path,
    out_path: str | Path,
    bgm: str | Path | None = None,
    voice: str | Path | None = None,
    bgm_volume: float = 0.18,
    spec: VideoSpec | None = None,
) -> Path:
    """Mix BGM and/or a narration track under the video's own audio."""
    spec = spec or VideoSpec()
    if not bgm and not voice:
        shutil.copy(video, out_path)
        return Path(out_path)

    args = ["-y", "-i", str(video)]
    parts, labels = [], []
    idx = 1
    if voice:
        args += ["-i", str(voice)]
        parts.append(f"[{idx}:a]volume=1.0,aresample=44100[voice]")
        labels.append("[voice]")
        idx += 1
    if bgm:
        # Loop the bed so a short track still covers a longer edit.
        args += ["-stream_loop", "-1", "-i", str(bgm)]
        parts.append(f"[{idx}:a]volume={bgm_volume},aresample=44100[bgm]")
        labels.append("[bgm]")
        idx += 1

    parts.append("[0:a]volume=1.0,aresample=44100[base]")
    labels.insert(0, "[base]")
    parts.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:"
        f"dropout_transition=0,alimiter=limit=0.95[aout]"
    )

    args += [
        "-filter_complex", ";".join(parts),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", spec.audio_bitrate,
        "-shortest", str(out_path),
    ]
    run_ffmpeg(args)
    return Path(out_path)


@dataclass(slots=True)
class ShotInput:
    """One cut: either a still to animate or existing footage to normalise."""

    duration: float
    image_path: str | None = None
    clip_path: str | None = None
    telop: str | None = None


def assemble_video(
    shots: list[ShotInput],
    out_path: str | Path,
    platform: Platform = Platform.TIKTOK,
    spec: VideoSpec | None = None,
    telop_style: TelopStyle | None = None,
    bgm: str | Path | None = None,
    voice: str | Path | None = None,
    ken_burns: bool = True,
    keep_workdir: Path | None = None,
) -> dict:
    """Build the finished video. This is the 'one touch' entry point.

    Returns a summary dict: output path, duration, shot count, telop cue count.
    """
    if not shots:
        raise FFmpegError("no shots supplied")

    spec = spec or PLATFORM_SPECS.get(platform, VideoSpec())
    telop_style = telop_style or PLATFORM_TELOP_STYLES.get(platform, TelopStyle())
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    workdir = Path(keep_workdir) if keep_workdir else Path(tempfile.mkdtemp(prefix="snsauto-"))
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        clips: list[Path] = []
        cues: list[TelopCue] = []
        cursor = 0.0

        for i, shot in enumerate(shots):
            dest = workdir / f"clip_{i:03d}.mp4"
            if shot.clip_path:
                normalize_clip(shot.clip_path, dest, spec, duration=shot.duration)
            elif shot.image_path:
                still_to_clip(shot.image_path, dest, shot.duration, spec, ken_burns)
            else:
                raise FFmpegError(f"shot {i} has neither image_path nor clip_path")

            actual = probe(dest)["duration"] or shot.duration
            if shot.telop:
                # Trim the tail slightly so telop swaps land on the cut.
                cues.append(TelopCue(cursor, cursor + max(0.3, actual - 0.08), shot.telop))
            cursor += actual
            clips.append(dest)

        stitched = concat_clips(clips, workdir / "stitched.mp4", workdir)

        current = stitched
        if cues:
            ass = workdir / "telop.ass"
            ass.write_text(
                build_ass(cues, telop_style, spec.width, spec.height), encoding="utf-8"
            )
            current = burn_telop(current, ass, workdir / "telop.mp4", spec)

        if bgm or voice:
            current = mix_audio(current, workdir / "mixed.mp4", bgm, voice, spec=spec)

        if spec.max_duration_sec and (probe(current)["duration"] or 0) > spec.max_duration_sec:
            trimmed = workdir / "trimmed.mp4"
            run_ffmpeg([
                "-y", "-i", str(current), "-t", f"{spec.max_duration_sec:.3f}",
                "-c", "copy", str(trimmed),
            ])
            current = trimmed

        shutil.copy(current, out_path)
        info = probe(out_path)
        return {
            "path": str(out_path),
            "duration_sec": info["duration"],
            "width": info["width"] or spec.width,
            "height": info["height"] or spec.height,
            "fps": int(info["fps"] or spec.fps),
            "shots": len(clips),
            "telop_cues": len(cues),
        }
    finally:
        if keep_workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)

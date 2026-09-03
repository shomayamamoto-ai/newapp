"""Narration (text to speech) and timing realignment.

The valuable part here is not the synthesis call - it is what happens after.
A script's beat timings are a guess made before anyone read the words aloud;
real speech never matches that guess. Rendering to the guessed timings either
cuts the narration off mid-sentence or leaves dead air.

So each line is synthesised separately, its **real** duration is measured, and
the storyboard is re-timed to the audio. The video ends up as long as the words
actually take, which is the only length that is ever right.

With no TTS configured the same realignment still runs, using a
characters-per-second estimate - the timings improve even without an API key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import get_settings
from ..media.ffmpeg import FFmpegError, probe, run_ffmpeg
from ..models import Storyboard

log = logging.getLogger(__name__)

# Padding after each line so cuts do not land on the final consonant.
LINE_GAP_SEC = 0.18
MIN_LINE_SEC = 0.8


class TtsError(RuntimeError):
    pass


AUDIO_SUFFIXES = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/ogg": ".ogg",
    "audio/opus": ".opus", "audio/webm": ".webm", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/flac": ".flac",
}


def suffix_for(content_type: str) -> str:
    return AUDIO_SUFFIXES.get(content_type.split(";")[0].strip().lower(), ".mp3")


@dataclass(slots=True)
class VoiceLine:
    index: int
    text: str
    path: str | None
    duration: float
    synthesized: bool


@dataclass
class VoiceTrack:
    lines: list[VoiceLine]
    path: str | None
    total_duration: float
    synthesized: bool

    def summary(self) -> dict:
        return {
            "synthesized": self.synthesized,
            "lines": len(self.lines),
            "total_duration_sec": round(self.total_duration, 2),
            "track": self.path,
        }


def estimate_duration(text: str, chars_per_sec: float = 7.0, speed: float = 1.0) -> float:
    """Reading time for narration, used when no synthesiser is configured."""
    clean = "".join(ch for ch in (text or "") if not ch.isspace())
    if not clean:
        return 0.0
    return max(MIN_LINE_SEC, len(clean) / max(0.5, chars_per_sec * max(0.1, speed)))


class HttpTtsProvider:
    """Generic TTS endpoint: POST text, get audio bytes or a URL back."""

    def __init__(self, endpoint: str, api_key: str | None = None,
                 voice: str | None = None, speed: float = 1.0,
                 client: httpx.Client | None = None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.voice = voice
        self.speed = speed
        self._client = client or httpx.Client(timeout=120.0)

    def synthesize(self, out_stem: Path, text: str) -> Path:
        """Write the audio next to ``out_stem`` and return the real path.

        Providers return wav, mp3, ogg or m4a depending on the vendor and the
        request, so the suffix comes from the response rather than being
        assumed - a wav named .mp3 confuses every downstream player.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"text": text, "input": text, "speed": self.speed}
        if self.voice:
            payload["voice"] = self.voice

        resp = self._client.post(self.endpoint, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise TtsError(f"tts failed {resp.status_code}: {resp.text[:300]}")

        out_stem.parent.mkdir(parents=True, exist_ok=True)
        content_type = resp.headers.get("content-type", "")
        if content_type.startswith("audio/"):
            out_path = out_stem.with_suffix(suffix_for(content_type))
            out_path.write_bytes(resp.content)
            return out_path
        out_path = out_stem.with_suffix(".mp3")

        import base64

        body = resp.json()
        from .imagegen import _find_first

        blob = _find_first(body, ("audio", "audio_base64", "b64_json", "data"))
        if blob:
            out_path.write_bytes(base64.b64decode(blob))
            return out_path
        url = _find_first(body, ("url", "audio_url", "output_url"))
        if url:
            audio = self._client.get(url, timeout=None)
            audio.raise_for_status()
            out_path.write_bytes(audio.content)
            return out_path
        raise TtsError(f"unrecognised tts response: {list(body)[:6]}")


def build_tts_provider(settings=None) -> HttpTtsProvider | None:
    settings = settings or get_settings()
    if (settings.tts_provider or "none").lower() in ("none", "", "off"):
        return None
    if not settings.tts_endpoint:
        log.warning("TTS_PROVIDER set but TTS_ENDPOINT is unset")
        return None
    return HttpTtsProvider(
        settings.tts_endpoint, settings.tts_api_key,
        settings.tts_voice, settings.tts_speed,
    )


class VoiceService:
    """Synthesises narration and re-times the storyboard to the real audio."""

    def __init__(self, session, settings=None, provider=None):
        self.session = session
        self.settings = settings or get_settings()
        self.provider = provider if provider is not None else build_tts_provider(self.settings)

    def narrate(
        self,
        storyboard: Storyboard,
        out_dir: str | Path,
        realign: bool = True,
    ) -> VoiceTrack:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        lines: list[VoiceLine] = []
        for shot in storyboard.shots:
            text = (shot.narration or "").strip()
            if not text:
                lines.append(VoiceLine(shot.index, "", None, 0.0, False))
                continue

            path, duration, ok = None, 0.0, False
            if self.provider is not None:
                stem = out_dir / f"line_{shot.index:03d}"
                try:
                    written = Path(self.provider.synthesize(stem, text))
                    duration = probe(written)["duration"]
                    path, ok = str(written), True
                except (TtsError, FFmpegError, OSError, httpx.HTTPError) as exc:
                    log.error("tts failed on shot %s: %s", shot.index, exc)

            if not ok:
                duration = estimate_duration(
                    text, self.settings.tts_chars_per_sec, self.settings.tts_speed
                )
            lines.append(VoiceLine(shot.index, text, path, duration, ok))

        if realign:
            self._realign(storyboard, lines)

        # Pad each line out to its shot's length so the narration stays locked
        # to the picture. Concatenating speech end-to-end would drift: a shot
        # held longer than its line would shift every later line early.
        lengths = {s.index: (s.end - s.start) for s in storyboard.shots}
        track_path, total = None, sum(lengths.values())
        if any(l.path for l in lines):
            track_path = str(self._concat(lines, lengths, out_dir / "narration.m4a"))
            total = probe(track_path)["duration"]

        self.session.flush()
        return VoiceTrack(
            lines=lines, path=track_path, total_duration=total,
            synthesized=any(l.synthesized for l in lines),
        )

    def _realign(self, storyboard: Storyboard, lines: list[VoiceLine]) -> None:
        """Re-tile the shots so each one lasts as long as its narration does."""
        by_index = {l.index: l for l in lines}
        cursor = 0.0
        for shot in storyboard.shots:
            line = by_index.get(shot.index)
            spoken = line.duration if line else 0.0
            # A shot with no narration keeps its authored length; a narrated one
            # gets the greater of the two so speech is never clipped.
            length = max(shot.duration or 0.0, spoken + LINE_GAP_SEC if spoken else 0.0)
            length = max(MIN_LINE_SEC, length)
            shot.start = round(cursor, 3)
            shot.end = round(cursor + length, 3)
            cursor = shot.end

    def _concat(
        self, lines: list[VoiceLine], lengths: dict[int, float], out_path: Path
    ) -> Path:
        """Join per-line audio, each padded to exactly its shot's length.

        The result is the same length as the video timeline, so line N always
        starts when shot N does.
        """
        parts, filters, idx = [], [], 0
        for line in lines:
            slot = lengths.get(line.index, line.duration)
            if line.path:
                # Trim guards against a synthesiser overrunning its slot, which
                # would otherwise push every later line late.
                parts += ["-i", line.path]
                filters.append(f"[{idx}:a]atrim=0:{slot:.3f},asetpts=PTS-STARTPTS,"
                               f"apad=whole_dur={slot:.3f},aresample=44100[a{idx}]")
                idx += 1
            else:
                parts += ["-f", "lavfi", "-t", f"{max(0.01, slot):.3f}",
                          "-i", "anullsrc=r=44100:cl=stereo"]
                filters.append(f"[{idx}:a]aresample=44100[a{idx}]")
                idx += 1

        chain = ";".join(filters)
        joined = "".join(f"[a{i}]" for i in range(idx))
        run_ffmpeg([
            "-y", *parts,
            "-filter_complex", f"{chain};{joined}concat=n={idx}:v=0:a=1[out]",
            "-map", "[out]", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            str(out_path),
        ])
        return out_path

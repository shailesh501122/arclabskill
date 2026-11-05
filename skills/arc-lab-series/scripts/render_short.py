#!/usr/bin/env python3
"""Arc Lab series: render a vertical (9:16) short-form video from a storyboard JSON.

Pipeline
  1. Read a storyboard (title, episode, scenes).
  2. Add a title card and an outro scene.
  3. Optionally synthesize narration per scene (espeak-ng / espeak / macOS `say`),
     or use a per-scene "audio" file you recorded yourself.
  4. Draw each scene's background and its caption chunks with Pillow.
  5. Composite every scene with ffmpeg (slow zoom, timed captions, progress bar),
     then join the scenes into one MP4. Also writes an .srt and a cover PNG.

Only Pillow and ffmpeg are required. No network access or API keys.

Usage
  python3 render_short.py storyboard.json -o out/episode-01.mp4 [--voice] [--music bed.mp3]
  python3 render_short.py --check
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: pip install Pillow")

W, H = 1080, 1920
FPS = 30
SAFE_X = 90                      # side margin for text
CAPTION_CENTER_Y = int(H * 0.56)  # captions sit a little below center
WORDS_PER_SEC = 2.6              # reading pace used when there is no narration

DEFAULT_THEME = {
    "bg_top": "#0B1020",
    "bg_bottom": "#1B1140",
    "accent": "#00E5FF",
    "accent2": "#FF3D81",
    "text": "#FFFFFF",
    "muted": "#9AA4C7",
}

FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
    "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/opentype/inter/Inter-Medium.otf",
    "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


# --------------------------------------------------------------------------- utils

def log(msg: str) -> None:
    print(f"[arc-lab] {msg}", file=sys.stderr)


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def fc_match(bold: bool) -> str | None:
    if not shutil.which("fc-match"):
        return None
    pattern = "sans:bold" if bold else "sans"
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out or None
    except Exception:
        return None


def find_font(override: str | None, bold: bool) -> str | None:
    if override and Path(override).is_file():
        return override
    for cand in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR):
        if Path(cand).is_file():
            return cand
    return fc_match(bold)


class Fonts:
    def __init__(self, bold_path: str | None, regular_path: str | None):
        self.bold_path = bold_path
        self.regular_path = regular_path or bold_path
        self._cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}

    def get(self, size: int, bold: bool = True):
        key = (bold, size)
        if key not in self._cache:
            path = self.bold_path if bold else self.regular_path
            if path:
                self._cache[key] = ImageFont.truetype(path, size)
            else:  # Pillow >= 10.1 ships a scalable default font
                self._cache[key] = ImageFont.load_default(size)
        return self._cache[key]


def run(cmd: list[str], quiet: bool = True) -> None:
    res = subprocess.run(cmd, capture_output=quiet, text=True)
    if res.returncode != 0:
        if quiet:
            sys.stderr.write(res.stderr[-4000:] if res.stderr else "")
        raise SystemExit(f"command failed: {' '.join(cmd[:6])} ...")


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# --------------------------------------------------------------------------- TTS

def detect_tts() -> str | None:
    for name in ("espeak-ng", "espeak", "say"):
        if shutil.which(name):
            return name
    return None


def synth(engine: str, text: str, out_wav: Path, voice: str | None, rate: int) -> bool:
    try:
        if engine in ("espeak-ng", "espeak"):
            cmd = [engine, "-s", str(rate), "-w", str(out_wav)]
            if voice:
                cmd += ["-v", voice]
            cmd.append(text)
            subprocess.run(cmd, check=True, capture_output=True)
        elif engine == "say":
            aiff = out_wav.with_suffix(".aiff")
            cmd = ["say", "-r", str(rate), "-o", str(aiff)]
            if voice:
                cmd += ["-v", voice]
            cmd.append(text)
            subprocess.run(cmd, check=True, capture_output=True)
            run(["ffmpeg", "-y", "-i", str(aiff), str(out_wav)])
        else:
            return False
        return out_wav.exists() and out_wav.stat().st_size > 0
    except Exception as exc:  # keep going without narration for this scene
        log(f"TTS failed ({exc}); scene will be silent")
        return False


# --------------------------------------------------------------------------- model

@dataclass
class Chunk:
    text: str
    start: float
    end: float


@dataclass
class Scene:
    kind: str                 # "title" | "body" | "outro"
    text: str                 # caption / narration text
    kicker: str = ""
    narration: str = ""
    duration: float = 0.0
    audio: Path | None = None
    chunks: list[Chunk] = field(default_factory=list)


def chunk_words(text: str, max_words: int, max_chars: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and (len(cur) >= max_words or len(trial) > max_chars):
            out.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
        # break after sentence-ending punctuation for natural pacing
        if re.search(r"[.!?;:]$", w) and cur:
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    # fold a lone trailing word ("ones.") into the chunk before it so it doesn't flash by
    merged: list[str] = []
    for part in out:
        if (merged and len(part.split()) == 1
                and not re.search(r"[.!?;:]$", merged[-1])
                and len(merged[-1]) + len(part) + 1 <= max_chars + 10):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def time_chunks(parts: list[str], start: float, end: float) -> list[Chunk]:
    weights = [max(len(p), 8) for p in parts]
    total = sum(weights) or 1
    t = start
    chunks = []
    for p, wgt in zip(parts, weights):
        d = (end - start) * wgt / total
        chunks.append(Chunk(p, t, t + d))
        t += d
    if chunks:
        chunks[-1].end = end
    return chunks


def build_scenes(sb: dict, base_dir: Path) -> list[Scene]:
    series = sb.get("series", "Arc Lab")
    ep = sb.get("episode")
    scenes: list[Scene] = []

    title = sb.get("title") or "Untitled"
    hook = sb.get("hook", "")
    scenes.append(Scene(
        kind="title",
        text=title,
        kicker=f"{series.upper()}" + (f"  ·  EP {int(ep):02d}" if ep is not None else ""),
        narration=sb.get("title_narration") or (f"{hook} " if hook else "") + title,
        duration=float(sb.get("title_duration", 0) or 0),
    ))

    for i, raw in enumerate(sb.get("scenes", []), 1):
        if isinstance(raw, str):
            raw = {"text": raw}
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        audio = raw.get("audio")
        scenes.append(Scene(
            kind="body",
            text=text,
            kicker=raw.get("kicker", ""),
            narration=raw.get("narration", text),
            duration=float(raw.get("duration", 0) or 0),
            audio=(base_dir / audio) if audio else None,
        ))

    outro = sb.get("outro", {})
    if outro is not False:
        if isinstance(outro, str):
            outro = {"text": outro}
        nxt = f"Episode {int(ep) + 1:02d}" if ep is not None else "the next episode"
        text = outro.get("text") or f"Follow {series} for {nxt}"
        scenes.append(Scene(
            kind="outro",
            text=text,
            kicker=outro.get("kicker", "NEXT UP"),
            narration=outro.get("narration", text),
            duration=float(outro.get("duration", 0) or 0),
        ))
    return scenes


# --------------------------------------------------------------------------- drawing

def gradient(top: tuple, bottom: tuple) -> Image.Image:
    col = Image.new("RGB", (1, H))
    px = col.load()
    for y in range(H):
        t = y / (H - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return col.resize((W, H))


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text: str, fonts: Fonts, max_w: int, max_h: int,
             start: int, minimum: int, max_lines: int, spacing: float = 1.12):
    size = start
    while size >= minimum:
        font = fonts.get(size)
        lines = wrap(draw, text, font, max_w)
        widest = max(draw.textlength(l, font=font) for l in lines) if lines else 0
        total_h = len(lines) * size * spacing
        if len(lines) <= max_lines and widest <= max_w and total_h <= max_h:
            return font, lines, size
        size -= 4
    font = fonts.get(minimum)
    return font, wrap(draw, text, font, max_w), minimum


def pill(draw, xy_center: tuple[int, int], text: str, font, fill, text_fill, pad=(28, 14)):
    tw = draw.textlength(text, font=font)
    asc, desc = font.getmetrics()
    th = asc + desc
    cx, cy = xy_center
    box = (cx - tw / 2 - pad[0], cy - th / 2 - pad[1], cx + tw / 2 + pad[0], cy + th / 2 + pad[1])
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) / 2, fill=fill)
    draw.text((cx, cy), text, font=font, fill=text_fill, anchor="mm")


def render_background(scene: Scene, idx: int, total: int, sb: dict, theme: dict,
                      fonts: Fonts, out: Path) -> None:
    img = gradient(hex_rgb(theme["bg_top"]), hex_rgb(theme["bg_bottom"])).convert("RGBA")

    # soft glow orbs, positioned differently per scene so cuts feel like movement
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    ang = idx * 1.7
    for color, r, ox, oy in ((theme["accent"], 520, 0.25, 0.30), (theme["accent2"], 460, 0.80, 0.78)):
        cx = int(W * (ox + 0.12 * math.cos(ang)))
        cy = int(H * (oy + 0.06 * math.sin(ang)))
        g.ellipse((cx - r, cy - r, cx + r, cy + r), fill=hex_rgb(color) + (70,))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.alpha_composite(img, glow)

    # subtle dot grid ("lab" texture)
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for y in range(40, H, 60):
        for x in range(30, W, 60):
            gd.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(255, 255, 255, 22))
    img = Image.alpha_composite(img, grid)

    d = ImageDraw.Draw(img)
    accent = hex_rgb(theme["accent"])
    muted = hex_rgb(theme["muted"])
    series = sb.get("series", "Arc Lab").upper()

    # header: logo mark + series name + scene counter
    d.arc((SAFE_X, 150, SAFE_X + 64, 214), start=200, end=520, fill=accent, width=10)
    d.text((SAFE_X + 88, 182), series, font=fonts.get(46), fill=hex_rgb(theme["text"]), anchor="lm")
    if scene.kind == "body":
        d.text((W - SAFE_X, 182), f"{idx:02d}/{total:02d}", font=fonts.get(40, bold=False),
               fill=muted, anchor="rm")

    handle = sb.get("handle")
    if handle:
        d.text((W // 2, H - 150), handle, font=fonts.get(38, bold=False), fill=muted, anchor="mm")

    if scene.kind == "title":
        if scene.kicker:
            pill(d, (W // 2, int(H * 0.30)), scene.kicker, fonts.get(40), accent, (10, 12, 30))
        font, lines, size = fit_text(d, scene.text, fonts, W - 2 * SAFE_X, int(H * 0.36),
                                     start=128, minimum=72, max_lines=5)
        line_h = int(size * 1.12)
        y0 = int(H * 0.52) - (len(lines) * line_h) // 2
        for i, line in enumerate(lines):
            d.text((W // 2, y0 + i * line_h + line_h // 2), line, font=font,
                   fill=hex_rgb(theme["text"]), anchor="mm",
                   stroke_width=4, stroke_fill=(0, 0, 0))
        sub = sb.get("subtitle")
        if sub:
            sfont, slines, ssize = fit_text(d, sub, fonts, W - 2 * SAFE_X, 220,
                                            start=48, minimum=34, max_lines=3)
            sy = y0 + len(lines) * line_h + 60
            for i, line in enumerate(slines):
                d.text((W // 2, sy + i * int(ssize * 1.25)), line, font=fonts.get(ssize, bold=False),
                       fill=muted, anchor="mt")
    elif scene.kicker:
        pill(d, (W // 2, int(H * 0.36)), scene.kicker.upper(), fonts.get(40),
             accent if scene.kind == "body" else hex_rgb(theme["accent2"]), (10, 12, 30))

    img.convert("RGB").save(out)


def render_caption(text: str, theme: dict, fonts: Fonts, out: Path) -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font, lines, size = fit_text(d, text, fonts, W - 2 * SAFE_X, int(H * 0.30),
                                 start=112, minimum=64, max_lines=3, spacing=1.15)
    line_h = int(size * 1.15)
    y0 = CAPTION_CENTER_Y - (len(lines) * line_h) // 2

    # highlight the most "important" word (longest, ignoring punctuation)
    words = re.findall(r"\S+", text)
    key = max(words, key=lambda w: len(re.sub(r"\W", "", w))) if words else ""
    accent = hex_rgb(theme["accent"])
    white = hex_rgb(theme["text"])

    for i, line in enumerate(lines):
        lw = d.textlength(line, font=font)
        x = (W - lw) / 2
        y = y0 + i * line_h
        for j, word in enumerate(line.split(" ")):
            piece = word + (" " if j < len(line.split(" ")) - 1 else "")
            fill = accent if word == key else white
            d.text((x, y), word, font=font, fill=fill, stroke_width=10, stroke_fill=(0, 0, 0))
            x += d.textlength(piece, font=font)
    img.save(out)


# --------------------------------------------------------------------------- video

def render_scene_video(scene: Scene, bg: Path, captions: list[Path], t0: float, total: float,
                       theme: dict, out: Path) -> None:
    D = scene.duration
    inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{D:.3f}", "-i", str(bg)]
    for c in captions:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{D:.3f}", "-i", str(c)]
    bar_idx = 1 + len(captions)
    accent = theme["accent"].lstrip("#")
    inputs += ["-f", "lavfi", "-t", f"{D:.3f}", "-i", f"color=c=0x{accent}:s={W}x14:r={FPS}"]
    audio_idx = bar_idx + 1
    if scene.audio:
        inputs += ["-i", str(scene.audio)]
    else:
        inputs += ["-f", "lavfi", "-t", f"{D:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]

    # slow push-in on the background; scale up then crop to frame
    zoom = 0.06 if scene.kind != "title" else 0.04
    f = [f"[0:v]scale=w='trunc({W}*(1+{zoom}*t/{D:.3f})/2)*2':h=-2:eval=frame,"
         f"crop={W}:{H},setsar=1,format=yuv420p[v0]"]
    last = "v0"
    for i, ch in enumerate(scene.chunks, 1):
        nxt = f"v{i}"
        f.append(f"[{last}][{i}:v]overlay=0:0:enable='between(t,{ch.start:.3f},{ch.end - 0.001:.3f})'[{nxt}]")
        last = nxt
    # progress bar across the whole video, pinned to the bottom edge
    f.append(f"[{last}][{bar_idx}:v]overlay=x='-w+w*({t0:.3f}+t)/{total:.3f}':y={H - 14}[vbar]")
    fade = []
    if scene.kind == "title":
        fade.append("fade=t=in:st=0:d=0.25")
    if scene.kind == "outro":
        fade.append(f"fade=t=out:st={max(D - 0.4, 0):.3f}:d=0.4")
    f.append(f"[vbar]{','.join(fade) if fade else 'null'},format=yuv420p[vout]")
    f.append(f"[{audio_idx}:a]aresample=48000,aformat=channel_layouts=stereo,apad,"
             f"atrim=0:{D:.3f}[aout]")

    cmd = ["ffmpeg", "-y", "-hide_banner", *inputs,
           "-filter_complex", ";".join(f),
           "-map", "[vout]", "-map", "[aout]",
           "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-t", f"{D:.3f}", "-movflags", "+faststart", str(out)]
    run(cmd)


def concat(parts: list[Path], out: Path, music: Path | None, music_volume: float, work: Path) -> None:
    listfile = work / "concat.txt"
    listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    joined = work / "joined.mp4" if music else out
    run(["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", "-movflags", "+faststart", str(joined)])
    if music:
        dur = probe_duration(joined)
        run(["ffmpeg", "-y", "-hide_banner", "-i", str(joined), "-stream_loop", "-1", "-i", str(music),
             "-filter_complex",
             f"[1:a]volume={music_volume},afade=t=out:st={max(dur - 1.5, 0):.3f}:d=1.5[m];"
             f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
             "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)])


# --------------------------------------------------------------------------- main

def check_env() -> int:
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        print(f"{tool:10s} {'OK  ' + path if path else 'MISSING (install ffmpeg)'}")
        ok &= bool(path)
    print(f"{'Pillow':10s} OK  {Image.__version__ if hasattr(Image, '__version__') else ''}")
    bold = find_font(None, True)
    print(f"{'font':10s} {bold or 'none found, using Pillow default font'}")
    tts = detect_tts()
    print(f"{'tts':10s} {tts or 'none (videos render silent unless you pass --music or per-scene audio)'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Render an Arc Lab 9:16 short from a storyboard JSON.")
    ap.add_argument("storyboard", nargs="?", help="path to storyboard .json")
    ap.add_argument("-o", "--output", help="output .mp4 (default: <storyboard>.mp4 next to it)")
    ap.add_argument("--voice", action="store_true", help="narrate each scene with local TTS if available")
    ap.add_argument("--tts-voice", default=None, help="voice name for the TTS engine (e.g. en-us, Samantha)")
    ap.add_argument("--tts-rate", type=int, default=170, help="TTS words per minute (default 170)")
    ap.add_argument("--music", help="background music file, looped and mixed under the voice")
    ap.add_argument("--music-volume", type=float, default=0.15)
    ap.add_argument("--font", help="path to a bold .ttf/.otf font to use")
    ap.add_argument("--max-words", type=int, default=4, help="max words per caption chunk")
    ap.add_argument("--keep-work", action="store_true", help="keep intermediate frames and clips")
    ap.add_argument("--dry-run", action="store_true", help="print the scene plan and exit")
    ap.add_argument("--check", action="store_true", help="check dependencies and exit")
    args = ap.parse_args()

    if args.check:
        return check_env()
    if not args.storyboard:
        ap.error("storyboard path is required (or use --check)")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool) and not args.dry_run:
            sys.exit(f"{tool} not found on PATH. Install ffmpeg first.")

    sb_path = Path(args.storyboard).resolve()
    sb = json.loads(sb_path.read_text(encoding="utf-8"))
    theme = {**DEFAULT_THEME, **(sb.get("theme") or {})}
    out = Path(args.output).resolve() if args.output else sb_path.with_suffix(".mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    fonts = Fonts(find_font(args.font or sb.get("font"), True), find_font(None, False))
    scenes = build_scenes(sb, sb_path.parent)
    if len(scenes) < 2:
        sys.exit("storyboard needs at least one scene with text")

    engine = detect_tts() if args.voice else None
    if args.voice and not engine:
        log("--voice requested but no TTS engine found (espeak-ng, espeak, say); rendering silent")

    work = Path(tempfile.mkdtemp(prefix="arclab-"))
    try:
        # 1. narration + durations
        for i, sc in enumerate(scenes):
            if sc.audio is None and engine and sc.narration:
                wav = work / f"vo_{i:02d}.wav"
                if synth(engine, sc.narration, wav, args.tts_voice, args.tts_rate):
                    sc.audio = wav
            if sc.audio is not None and not sc.audio.exists():
                log(f"audio file not found: {sc.audio}; scene {i} will be silent")
                sc.audio = None
            reading = len(sc.text.split()) / WORDS_PER_SEC + 0.8
            base = max(2.2 if sc.kind != "body" else 2.0, reading)
            if sc.audio is not None:
                base = max(base if sc.kind == "title" else 0, probe_duration(sc.audio) + 0.35)
            sc.duration = sc.duration or round(base, 2)

        total = sum(s.duration for s in scenes)

        # 2. caption chunks (title card shows its text statically, no chunks)
        for sc in scenes:
            if sc.kind == "title":
                continue
            lead = 0.15
            sc.chunks = time_chunks(chunk_words(sc.text, args.max_words, 22), lead, sc.duration)

        if args.dry_run:
            t = 0.0
            for i, sc in enumerate(scenes):
                print(f"{i:02d} {sc.kind:5s} {t:6.2f}s +{sc.duration:5.2f}s  {sc.text[:60]}")
                for ch in sc.chunks:
                    print(f"       {ch.start:5.2f}-{ch.end:5.2f}  {ch.text}")
                t += sc.duration
            print(f"total {total:.2f}s")
            return 0

        # 3. render
        parts: list[Path] = []
        srt: list[str] = []
        body_total = sum(1 for s in scenes if s.kind == "body")
        body_idx = 0
        t0 = 0.0
        for i, sc in enumerate(scenes):
            if sc.kind == "body":
                body_idx += 1
            bg = work / f"bg_{i:02d}.png"
            render_background(sc, body_idx, body_total, sb, theme, fonts, bg)
            if sc.kind == "title":
                shutil.copy(bg, out.with_name(out.stem + "_cover.png"))
            caps = []
            for j, ch in enumerate(sc.chunks):
                cp = work / f"cap_{i:02d}_{j:02d}.png"
                render_caption(ch.text, theme, fonts, cp)
                caps.append(cp)
                srt.append(f"{len(srt) + 1}\n{srt_time(t0 + ch.start)} --> {srt_time(t0 + ch.end)}\n{ch.text}\n")
            clip = work / f"scene_{i:02d}.mp4"
            log(f"scene {i + 1}/{len(scenes)} ({sc.kind}, {sc.duration:.1f}s)")
            render_scene_video(sc, bg, caps, t0, total, theme, clip)
            parts.append(clip)
            t0 += sc.duration

        music = Path(args.music).resolve() if args.music else None
        if music and not music.exists():
            log(f"music file not found: {music}; skipping")
            music = None
        concat(parts, out, music, args.music_volume, work)
        out.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8")
        log(f"done: {out}  ({probe_duration(out):.1f}s, {W}x{H})")
        log(f"captions: {out.with_suffix('.srt')}   cover: {out.with_name(out.stem + '_cover.png')}")
        print(out)
        return 0
    finally:
        if args.keep_work:
            log(f"work files kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

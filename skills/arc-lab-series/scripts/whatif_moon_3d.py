#!/usr/bin/env python3
"""Arc Lab "What If?" Moon episode, 3D version (9:16).

The 3D world (low-poly seaside bay, space view, Kai as a 3D character) lives in
moon3d/scene.html and is rendered with three.js in headless Chromium. This script then
adds the 2D overlays from whatif_moon.py (time counter, animated captions, brand, CTA,
tide gauge, labels) and the same narration + music mix.

Usage
  python3 whatif_moon_3d.py --narration narration.mp3 -o what-if-moon-3d.mp4 [--workers 3]
  python3 whatif_moon_3d.py --narration narration.mp3 --still 20 -o check.png
Requires: node, the Playwright Chromium (require('playwright')), ffmpeg, Pillow, numpy.
three.js is installed into moon3d/node_modules on first run.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dance_game as dg   # noqa: E402
import whatif_moon as wm  # noqa: E402

W, H, FPS, SS = wm.W, wm.H, wm.FPS, wm.SS
SCENE_DIR = HERE / "moon3d"
THREE_VER = "0.169.0"


def prepare(work: Path):
    three = SCENE_DIR / "node_modules" / "three" / "build" / "three.module.min.js"
    if not three.exists():
        subprocess.run(["npm", "install", "--prefix", str(SCENE_DIR), f"three@{THREE_VER}", "--silent"], check=True)
    shutil.copy(three, work / "three.js")
    shutil.copy(SCENE_DIR / "scene.html", work / "scene.html")
    shutil.copy(SCENE_DIR / "render_frames.js", work / "render_frames.js")
    data = {"segments": [{"scene": s["scene"], "start": s["start"], "end": s["end"]} for s in wm.SEGMENTS],
            "vanish": wm.VANISH_T, "total": wm.TOTAL}
    (work / "data.js").write_text("window.ARC_DATA = " + json.dumps(data) + ";\n")


def render_3d(work: Path, frames: Path, n: int, workers: int, start: int = 0):
    frames.mkdir(parents=True, exist_ok=True)
    k = max(1, workers)
    procs = []
    span = n - start
    for i in range(k):
        a = start + span * i // k
        b = start + span * (i + 1) // k
        if b > a:
            procs.append(subprocess.Popen(["node", str(work / "render_frames.js"), str(work), str(frames),
                                           str(a), str(b), str(FPS)]))
    for p in procs:
        if p.wait() != 0:
            sys.exit("3D frame render failed")
    tilt = {}
    for f in frames.glob("info_*.json"):
        for i, ang in json.loads(f.read_text()):
            tilt[i] = ang
    return tilt


def overlay(img3d: Image.Image, t: float, tilt) -> Image.Image:
    img = img3d.convert("RGB").resize((W * SS, H * SS), Image.BILINEAR)
    cv = dg.Cv(img)
    i = wm.scene_at(t)
    seg = wm.SEGMENTS[i]
    dt = t - seg["start"]
    name = seg["scene"]
    if name == "tides":
        wm.tide_gauge(cv, t, dt)
    if name == "seasons":
        wm.text(cv, (270, 560), "BRUTAL WINTERS", 46, fill=(40, 90, 160), stroke=0)
        wm.text(cv, (810, 620), "SCORCHING SUMMERS", 42, fill=(150, 50, 10), stroke=0)
    if name in ("wobble", "stabilizer") and tilt is not None:
        wm.text(cv, (540, 1110), f"TILT {tilt:4.1f}°", 54, fill=(255, 220, 90), stroke=6)
    if name == "stabilizer":
        wm.text(cv, (540, 330), "THE MOON = EARTH'S STABILIZER", 44, fill=(255, 214, 64), stroke=6)
    wm.counter_hud(cv, t, i)
    if name == "cta":
        wm.cta(cv, t)
    wm.captions(cv, t)
    wm.brand(cv)
    cv.rect((0, H - 10, W * t / wm.TOTAL, H), (255, 214, 64))
    out = img.reduce(SS)
    if t < 0.2:
        out = Image.blend(Image.new("RGB", out.size), out, t / 0.2)
    if t > wm.TOTAL - 0.5:
        out = Image.blend(out, Image.new("RGB", out.size), (t - (wm.TOTAL - 0.5)) / 0.5)
    return out


def composite_range(args):
    a, b, frames, tilt, path = args
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(a, b):
        im = Image.open(frames / f"f{i:05d}.jpg")
        proc.stdin.write(overlay(im, i / FPS, tilt.get(i)).tobytes())
    proc.stdin.close()
    proc.wait()
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--narration", required=True)
    ap.add_argument("-o", "--output", default="what-if-moon-3d.mp4")
    ap.add_argument("--still", type=float)
    ap.add_argument("--workers", type=int, default=3, help="parallel Chromium renderers")
    args = ap.parse_args()
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="moon3d-"))
    try:
        prepare(work)
        frames = work / "frames"
        if args.still is not None:
            i = int(round(args.still * FPS))
            tilt = render_3d(work, frames, i + 1, 1, start=i)
            overlay(Image.open(frames / f"f{i:05d}.jpg"), i / FPS, tilt.get(i)).save(out.with_suffix(".png"))
            print(out.with_suffix(".png"))
            return 0
        n = int(wm.TOTAL * FPS)
        print(f"[3d] rendering {n} frames with {args.workers} browsers", file=sys.stderr)
        tilt = render_3d(work, frames, n, args.workers)
        wav = work / "mix.wav"
        wm.soundtrack(Path(args.narration), wav, work)
        k = os.cpu_count() or 2
        bounds = [(n * j // k, n * (j + 1) // k, frames, tilt, work / f"p{j}.mp4") for j in range(k)]
        with mp.get_context("fork").Pool(k) as pool:
            parts = pool.map(composite_range, bounds)
        (work / "l.txt").write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(work / "l.txt"),
                        "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-t", f"{wm.TOTAL:.3f}", "-movflags", "+faststart", str(out)], check=True)
        overlay(Image.open(frames / f"f{int(1.0 * FPS):05d}.jpg"), 1.0, None).save(out.with_name(out.stem + "_cover.png"))
        print(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

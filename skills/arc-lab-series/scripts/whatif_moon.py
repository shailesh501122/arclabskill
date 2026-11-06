#!/usr/bin/env python3
"""Arc Lab "What If?" explainer short (9:16): What if the Moon vanished tonight?

Format: one code-drawn location (a seaside town at night) that changes as time passes,
Kai from the Arc Lab series reacting in every scene, a "TIME WITHOUT THE MOON" counter
with a status tag, 2-word animated captions with highlighted keywords, narration
(an ElevenLabs take, split by its pauses), a synthesized music bed with SFX, and a
yes/no comment question at the end.

Usage
  python3 whatif_moon.py --narration narration.mp3 -o what-if-moon.mp4 [--workers 4]
  python3 whatif_moon.py --narration narration.mp3 --still 20 -o check.png
"""
from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import os
import random
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import animate_arc as rig  # noqa: E402
import dance_game as dg    # noqa: E402  (Cv canvas, XCanvas character transform, synth helpers)

W, H = 1080, 1920
SS = dg.SS
FPS = 30
INK = (17, 16, 28)

# ------------------------------------------------------------------ script + timing
# Each segment: narration line(s) with the audio interval [a, b] found by silence detection.
# "parts" splits a segment at its internal pauses so caption timing stays tight.
SEGMENTS = [
    dict(scene="hook", counter=("0", "SECONDS"), status="MOON: GONE",
         parts=[(0.00, 2.40, "What if the Moon just... vanished?"), (2.93, 3.31, "Tonight.")],
         keys={"Moon", "vanished?", "Tonight."}),
    dict(scene="empty", counter=("1", "SECOND"), status="SKY: EMPTIER",
         parts=[(4.67, 6.84, "One second later, nothing seems wrong."),
                (7.31, 9.04, "The sky just looks a little emptier.")],
         keys={"nothing", "emptier."}),
    dict(scene="tides", counter=("1", "DAY"), status="TIDES: ~1/3 SIZE",
         parts=[(10.24, 12.41, "But within a day, the oceans notice."),
                (12.87, 17.65, "Tides shrink to about a third of their size, because now only the Sun is pulling on the water.")],
         keys={"oceans", "Tides", "third", "Sun"}),
    dict(scene="dark", counter=("1", "WEEK"), status="NIGHTS: PITCH BLACK",
         parts=[(18.74, 20.31, "Nights turn pitch black."),
                (20.78, 23.80, "With no moonlight, you'd see more stars than ever before.")],
         keys={"pitch", "black.", "stars"}),
    dict(scene="animals", counter=("1", "MONTH"), status="WILDLIFE: CONFUSED",
         parts=[(24.90, 26.55, "Then animals get confused."),
                (27.01, 31.29, "Baby sea turtles, corals and night hunters all time their lives by the Moon.")],
         keys={"confused.", "turtles,", "corals", "Moon."}),
    dict(scene="wobble", counter=("1M", "YEARS"), status="TILT: WOBBLING",
         parts=[(32.38, 37.29, "Over millions of years, Earth's tilt could start to wobble, because the Moon is what holds it steady.")],
         keys={"millions", "tilt", "wobble,", "steady."}),
    dict(scene="seasons", counter=("10M", "YEARS"), status="SEASONS: EXTREME",
         parts=[(38.41, 42.25, "That wobble could bring brutal winters... and scorching summers.")],
         keys={"brutal", "winters...", "scorching", "summers."}),
    dict(scene="stabilizer", counter=None, status=None,
         parts=[(43.35, 45.12, "The Moon isn't just a light in the sky."),
                (45.58, 46.88, "It's Earth's stabilizer.")],
         keys={"light", "stabilizer."}),
    dict(scene="cta", counter=None, status=None,
         parts=[(47.89, 50.52, "Would you miss the Moon? Comment yes or no!")],
         keys={"miss", "yes", "no!"}),
]
TOTAL = 54.0
VANISH_T = 2.0
BRAND = ("ARC LAB", "WHAT IF?")

# Optional: a different narration take with its own timings, as JSON
# {"total": 72, "vanish": 3.1, "segments": [{"scene", "counter": [num, unit] | null, "status",
#   "parts": [[a, b, "text"], ...], "keys": [...]}]}. Point WHATIF_EPISODE at the file.
if os.environ.get("WHATIF_EPISODE"):
    import json as _json
    _ep = _json.loads(Path(os.environ["WHATIF_EPISODE"]).read_text())
    TOTAL, VANISH_T = float(_ep["total"]), float(_ep["vanish"])
    BRAND = tuple(_ep.get("brand", BRAND))
    SEGMENTS = [dict(scene=x["scene"], counter=tuple(x["counter"]) if x.get("counter") else None,
                     status=x.get("status"), parts=[tuple(q) for q in x["parts"]], keys=set(x.get("keys", [])))
                for x in _ep["segments"]]

# scene start times: each scene begins a little before its narration
for i, s in enumerate(SEGMENTS):
    s["start"] = 0.0 if i == 0 else SEGMENTS[i]["parts"][0][0] - 0.35
for i, s in enumerate(SEGMENTS):
    s["end"] = SEGMENTS[i + 1]["start"] if i + 1 < len(SEGMENTS) else TOTAL


def build_words():
    """[(t0, t1, word, is_key)] using pause-split parts and length-weighted timing."""
    out = []
    for seg in SEGMENTS:
        for a, b, text in seg["parts"]:
            words = text.split()
            wts = [len(w) + 2 for w in words]
            tot = sum(wts)
            t = a
            for w, k in zip(words, wts):
                d = (b - a) * k / tot
                out.append((t, t + d, w, w in seg["keys"]))
                t += d
    return out


WORDS = build_words()


def caption_chunks():
    """Group words two at a time, never across a part boundary."""
    chunks = []
    for seg in SEGMENTS:
        for a, b, text in seg["parts"]:
            ws = [w for w in WORDS if a - 1e-6 <= w[0] < b - 1e-6]
            for i in range(0, len(ws), 2):
                grp = ws[i:i + 2]
                chunks.append((grp[0][0], grp[-1][1], grp))
    return chunks


CHUNKS = caption_chunks()


def scene_at(t):
    for i, s in enumerate(SEGMENTS):
        if s["start"] <= t < s["end"]:
            return i
    return len(SEGMENTS) - 1


# ------------------------------------------------------------------ fonts

def _font(paths, size):
    for p in paths:
        if Path(p).is_file():
            return ImageFont.truetype(p, int(size * SS))
    return ImageFont.load_default(int(size * SS))


MONO = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/System/Library/Fonts/Menlo.ttc", "C:/Windows/Fonts/consolab.ttf"]
SERIF = ["/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Georgia Bold.ttf", "C:/Windows/Fonts/georgiab.ttf"]
_FC = {}


def fnt(kind, size):
    size = max(6, int(size))
    key = (kind, size)
    if key not in _FC:
        _FC[key] = _font(MONO if kind == "mono" else SERIF, size) if kind in ("mono", "serif") else dg.F(size)
    return _FC[key]


def text(cv, xy, s, size, fill=(255, 255, 255), anchor="mm", kind="sans", stroke=0, stroke_fill=INK):
    cv.d.text((xy[0] * SS, xy[1] * SS), s, font=fnt(kind, size), fill=fill, anchor=anchor,
              stroke_width=int(stroke * SS), stroke_fill=stroke_fill)


def tlen(cv, s, size, kind="sans"):
    return cv.d.textlength(s, font=fnt(kind, size)) / SS


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)


def ease_out_back(t, s=1.7):
    t = clamp(t) - 1
    return t * t * ((s + 1) * t + s) + 1


def mixc(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ------------------------------------------------------------------ world

HORIZON = 1060
SHORE = 1370


def gradient_img(stops, h0, h1):
    """Vertical gradient between (pos, color) stops, as an RGB image of the given band."""
    hh = (h1 - h0) * SS
    ys = np.linspace(0, 1, hh)
    arr = np.zeros((hh, 3))
    pos = [p for p, _ in stops]
    for c in range(3):
        arr[:, c] = np.interp(ys, pos, [col[c] for _, col in stops])
    arr = np.repeat(arr.astype(np.uint8)[:, None, :], W * SS, axis=1)
    return Image.fromarray(arr, "RGB")


STARS = [(random.Random(i).uniform(0, W), random.Random(i * 3 + 1).uniform(0, HORIZON - 40),
          random.Random(i * 7 + 2).uniform(0.8, 2.6), random.Random(i * 11).random()) for i in range(1100)]


def make_glow(r, color):
    size = int(r * 4 * SS)
    g = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    c = size / 2
    d.ellipse((c - r * 1.3 * SS, c - r * 1.3 * SS, c + r * 1.3 * SS, c + r * 1.3 * SS), fill=color + (90,))
    return g.filter(ImageFilter.GaussianBlur(r * 0.45 * SS))


MOON_GLOW = None


def draw_moon(img, cv, c, r, alpha=1.0):
    global MOON_GLOW
    if alpha <= 0.01 or r < 1:
        return
    if MOON_GLOW is None:
        MOON_GLOW = make_glow(110, (230, 235, 255))
    g = MOON_GLOW
    if r != 110:
        s = int(g.size[0] * r / 110)
        g = g.resize((max(1, s), max(1, s)))
    if alpha < 1:
        g = g.copy()
        g.putalpha(g.getchannel("A").point(lambda v: int(v * alpha)))
    img.paste(g, (int(c[0] * SS - g.size[0] / 2), int(c[1] * SS - g.size[1] / 2)), g)
    a = int(255 * alpha)
    cv.circle(c, r, (246, 244, 230, a))
    for dx, dy, cr in ((-0.35, -0.2, 0.22), (0.3, 0.25, 0.16), (0.1, -0.45, 0.1), (-0.15, 0.4, 0.12), (0.45, -0.15, 0.09)):
        cv.circle((c[0] + dx * r, c[1] + dy * r), cr * r, (214, 212, 200, a))


def world(img, t, p):
    """Draw the seaside town. p: dict of scene parameters (all optional)."""
    cv = dg.Cv(img)
    dark = p.get("dark", 0.0)        # 0 normal night, 1 pitch black
    stars = p.get("stars", 160)
    tide_amp = p.get("tide_amp", 55)
    tide_speed = p.get("tide_speed", 0.25)
    tint = p.get("tint")             # optional (color, strength) wash for seasons
    moon = p.get("moon", 0.0)        # 0..1 moon visibility
    moon_r = p.get("moon_r", 110)

    top = mixc((14, 18, 52), (2, 2, 8), dark)
    hor = mixc((48, 62, 128), (8, 10, 24), dark)
    if moon > 0:
        hor = mixc(hor, (70, 88, 160), 0.4 * moon)
    img.paste(gradient_img([(0, top), (1, hor)], 0, HORIZON), (0, 0))
    # stars (+ Milky Way band when it gets dark)
    for i, (x, y, r, ph) in enumerate(STARS[:int(stars)]):
        tw = 0.6 + 0.4 * math.sin(t * 3 + ph * 20)
        a = int(255 * tw * (0.55 + 0.45 * dark))
        cv.circle((x, y), r * (1 + 0.3 * dark), (255, 255, 255, a))
    if dark > 0.05:
        rnd = random.Random(9)
        for i in range(int(900 * dark)):
            u = rnd.random()
            x = u * W
            y = 120 + u * 700 + rnd.gauss(0, 70)
            cv.circle((x, y), rnd.uniform(0.6, 1.6), (200, 210, 255, int(rnd.uniform(40, 150) * dark)))
    if moon > 0:
        draw_moon(img, cv, (760, 400), moon_r, moon)

    # far hills + town lights
    hill = mixc((24, 30, 64), (6, 6, 14), dark)
    cv.poly([(0, HORIZON - 70), (160, HORIZON - 120), (330, HORIZON - 80), (470, HORIZON - 30),
             (470, HORIZON + 2), (0, HORIZON + 2)], hill)
    rnd = random.Random(4)
    for i in range(26):
        x = rnd.uniform(20, 430)
        y = HORIZON - rnd.uniform(14, 70) * (1 - abs(x - 170) / 400)
        lit = rnd.random() < (0.9 - 0.6 * dark)
        cv.rect((x, y, x + 14, y + 12), (255, 214, 120, 230) if lit else (30, 30, 50))

    # ocean
    sea_top = mixc((26, 46, 100), (4, 6, 16), dark)
    sea_bot = mixc((14, 28, 70), (2, 4, 10), dark)
    tide = math.sin(t * 2 * math.pi * tide_speed) * tide_amp
    shore = SHORE - tide
    img.paste(gradient_img([(0, sea_top), (1, sea_bot)], HORIZON, int(shore) + 40), (0, HORIZON * SS))
    cv = dg.Cv(img)
    if moon > 0.05:  # moonlight path
        for k in range(26):
            y = HORIZON + 8 + k * (shore - HORIZON) / 26
            w = 30 + k * 6 + 14 * math.sin(t * 3 + k)
            cv.rect((760 - w / 2 + 10 * math.sin(t * 2 + k * 1.3), y, 760 + w / 2, y + 4),
                    (230, 235, 255, int(150 * moon * (1 - k / 30))))
    for k in range(9):  # wave lines
        y = HORIZON + 30 + k * (shore - HORIZON - 30) / 9
        off = (t * 40 + k * 70) % 160
        for x in range(-160, W + 160, 160):
            cv.line([(x + off, y), (x + off + 70, y)], (120, 150, 220, int(60 * (1 - dark))), 3)

    # beach
    sand = mixc((58, 52, 70), (12, 11, 16), dark)
    cv.poly([(0, shore), (W, shore - 30), (W, 1640), (0, 1660)], sand)
    foam = [(x, shore - 30 * x / W + 6 * math.sin(x / 40 + t * 3)) for x in range(0, W + 20, 20)]
    cv.line(foam, (210, 225, 255, int(170 * (1 - dark * 0.7))), 6)

    # lighthouse on the right cliff
    cliff = mixc((34, 34, 52), (8, 8, 12), dark)
    cv.poly([(760, 1640), (820, 1220), (930, 1180), (W, 1170), (W, 1640)], cliff)
    tx = 940
    cv.poly([(tx - 34, 1190), (tx + 34, 1190), (tx + 22, 950), (tx - 22, 950)], (236, 232, 226))
    for y in (1150, 1070, 990):
        cv.poly([(tx - 32 + (1190 - y) * 0.05, y), (tx + 32 - (1190 - y) * 0.05, y),
                 (tx + 30 - (1190 - y + 30) * 0.05, y - 30), (tx - 30 + (1190 - y + 30) * 0.05, y - 30)], (220, 60, 70))
    cv.rect((tx - 30, 912, tx + 30, 952), (40, 40, 56))
    cv.rect((tx - 22, 918, tx + 22, 948), (255, 230, 140))
    cv.poly([(tx - 36, 912), (tx + 36, 912), (tx, 880)], (40, 40, 56))
    a = t * 1.3
    bx = math.cos(a)
    if bx > -0.2:
        cv.poly([(tx, 932), (tx - 900 * bx, 900), (tx - 900 * bx, 965)], (255, 240, 170, int(55 * (bx + 0.2))))

    # foreground rock (Kai's stage)
    rock = mixc((22, 22, 36), (4, 4, 8), dark)
    cv.poly([(0, 1630), (120, 1600), (420, 1640), (620, 1700), (700, 1920), (0, 1920)], rock)
    cv.poly([(560, 1920), (760, 1640), (1080, 1600), (1080, 1920)], mixc(rock, (0, 0, 0), 0.3))
    if tint:
        col, s = tint
        wash = Image.new("RGB", img.size, col)
        img.paste(Image.blend(img, wash, s))
    return dg.Cv(img)


# ------------------------------------------------------------------ Kai

KAI = {"hoodie": "#FF7A1A", "pants": "#2B2D42", "skin": "#F4C7A1", "hair": "#22204A",
       "band": "#E8283C", "shoe": "#F5F5F5", "aura": "#3DF5FF", "accent": "#FFD23F"}


def kai(img, t, pose_name, origin=(300, 1790), k=0.68, mirror=False, jitter=0.0, flashlight=False):
    G = rig.G
    bob = 4 * math.sin(t * 2.4)
    p = rig.Pose(hip=(540, G - 214 + bob), lean=2, tilt=-4, eye=1.0, brow=0.2, mouth="smile", wind=0.4)
    if int(t * 10) % 41 == 0:
        p.eye = 0.1
    sh = rig.shoulder_of(p)
    hands = [(sh[0] + 40, sh[1] + 140), (sh[0] - 20, sh[1] + 140)]
    feet = [(570, G - 14), (512, G - 14)]
    if pose_name == "point_up":
        hands[0] = (sh[0] + 110, sh[1] - 140)
        p.tilt, p.mouth = -24, "o"
    elif pose_name == "shrug":
        s = 0.5 + 0.5 * math.sin(t * 3)
        hands = [(sh[0] + 90, sh[1] + 20 - 20 * s), (sh[0] - 70, sh[1] + 20 - 20 * s)]
        p.tilt, p.brow, p.mouth = -16, -0.6, "flat"
    elif pose_name == "think":
        hands[0] = (sh[0] + 56, sh[1] - 66)
        p.tilt, p.brow, p.mouth = 4, -0.4, "flat"
    elif pose_name == "flashlight":
        hands[0] = (sh[0] + 130, sh[1] + 10)
        p.tilt, p.mouth, p.brow, p.eye = -6, "o", -0.5, 1.0
    elif pose_name == "scratch":
        hands[0] = (sh[0] + 20, sh[1] - 120 + 8 * math.sin(t * 18))
        p.tilt, p.brow, p.mouth = 8, -0.8, "flat"
    elif pose_name == "float":
        hands = [(sh[0] + 90, sh[1] - 40 + 20 * math.sin(t * 2)), (sh[0] - 70, sh[1] - 30)]
        feet = [(600, G - 30 + 20 * math.sin(t * 2 + 1)), (500, G - 10)]
        p.mouth = "o"
    elif pose_name == "shiver":
        hands = [(sh[0] + 60, sh[1] + 60), (sh[0] + 10, sh[1] + 70)]
        p.brow, p.mouth, p.blush, p.sweat = -0.8, "grit", 0.6, 0.7
    elif pose_name == "thumbs":
        hands[0] = (sh[0] + 120, sh[1] - 60)
        p.mouth, p.brow = "smile", 0.6
    elif pose_name == "wave":
        hands[0] = (sh[0] + 90 + 30 * math.sin(t * 9), sh[1] - 150)
        p.mouth = "smile"
    p.hands, p.feet = hands, feet
    o = (origin[0] + jitter * math.sin(t * 80), origin[1])
    xc = dg.XCanvas(img, o, k, mirror)
    if flashlight:
        hx, hy = xc.P(hands[0])
        cvs = dg.Cv(img)
        x0, y0 = hx / SS, hy / SS
        d = -1 if mirror else 1
        cvs.poly([(x0, y0), (x0 + d * 760, y0 - 380), (x0 + d * 760, y0 + 160)], (255, 245, 190, 60))
    rig.draw_character(xc, p, KAI, t)


# ------------------------------------------------------------------ space scenes

def space(img, t, tilt_wobble=0.0, show_moon=False, glow=0.0):
    cv = dg.Cv(img)
    img.paste(gradient_img([(0, (4, 4, 14)), (1, (12, 10, 34))], 0, H), (0, 0))
    cv = dg.Cv(img)
    for (x, y, r, ph) in STARS[:500]:
        cv.circle((x, y * 1.8), r, (255, 255, 255, int(160 + 90 * math.sin(t * 2 + ph * 10))))
    c, R = (540, 860), 300
    if glow:
        g = make_glow(R, (90, 200, 255))
        g.putalpha(g.getchannel("A").point(lambda v: int(v * glow)))
        img.paste(g, (int(c[0] * SS - g.size[0] / 2), int(c[1] * SS - g.size[1] / 2)), g)
    cv.circle(c, R + 10, (90, 170, 255, 60))
    cv.circle(c, R, (40, 110, 220))
    rnd = random.Random(2)
    spin = t * 25
    for i in range(9):  # continents drifting across the disc
        a = rnd.uniform(0, 360) + spin
        lat = rnd.uniform(-0.6, 0.6)
        x = math.cos(math.radians(a))
        if x < -0.1:
            continue
        cx = c[0] + math.sin(math.radians(a)) * R * math.cos(lat * 1.2) * 0.9
        cy = c[1] + lat * R
        rr = rnd.uniform(40, 90) * (0.4 + 0.6 * x)
        cv.ellipse((cx - rr * (0.5 + 0.5 * x), cy - rr * 0.7, cx + rr * (0.5 + 0.5 * x), cy + rr * 0.7),
                   fill=(70, 170, 90))
    cv.ellipse((c[0] - 110, c[1] - R + 4, c[0] + 110, c[1] - R + 44), fill=(240, 248, 255, 200))
    cv.ellipse((c[0] - 90, c[1] + R - 36, c[0] + 90, c[1] + R - 4), fill=(240, 248, 255, 200))
    # night side shading
    cv.d.chord(((c[0] - R) * SS, (c[1] - R) * SS, (c[0] + R) * SS, (c[1] + R) * SS), 90, 270, fill=(0, 0, 20, 110))
    # axis
    base = 23.4
    ang = base + tilt_wobble * (18 * math.sin(t * 1.6) + 9 * math.sin(t * 3.7))
    a = math.radians(ang)
    dx, dy = math.sin(a) * (R + 130), -math.cos(a) * (R + 130)
    cv.line([(c[0] - dx, c[1] - dy), (c[0] + dx, c[1] + dy)], (255, 255, 255, 220), 6)
    cv.line([(c[0], c[1] - R - 150), (c[0], c[1] - R - 40)], (255, 255, 255, 90), 3)
    text(cv, (c[0] + dx + 10, c[1] + dy - 40), f"TILT {ang:4.1f}°", 44, fill=(255, 220, 90), stroke=6)
    if show_moon:
        oa = t * 0.9
        mx, my = c[0] + math.cos(oa) * 470, c[1] + math.sin(oa) * 160
        cv.ellipse((c[0] - 470, c[1] - 160, c[0] + 470, c[1] + 160), outline=(255, 255, 255, 70), width=3)
        if math.sin(oa) < 0:
            pass
        draw_moon(img, dg.Cv(img), (mx, my), 60, 1.0)
    return dg.Cv(img)


# ------------------------------------------------------------------ overlays

def counter_hud(cv, t, seg_i):
    seg = SEGMENTS[seg_i]
    if not seg["counter"]:
        return
    dt = t - seg["start"]
    pop = ease_out_back(dt / 0.35)
    num, unit = seg["counter"]
    x, y = 70, 150
    text(cv, (x, y), "TIME WITHOUT THE MOON", 26, fill=(230, 232, 245), anchor="lm", kind="sans", stroke=4)
    size = 96 * (0.7 + 0.3 * pop)
    text(cv, (x, y + 78), num, size, anchor="lm", kind="serif", stroke=6)
    nw = tlen(cv, num, size, "serif")
    text(cv, (x + nw + 16, y + 92), unit, 50, anchor="lm", kind="serif", stroke=5)
    st = seg["status"]
    if seg_i == 0 and t < VANISH_T + 0.3:
        st = None
    if st:
        w = tlen(cv, st, 26) + 70
        cv.rect((x, y + 150, x + w * clamp(dt / 0.3), y + 196), (16, 16, 30, 210), radius=12)
        if dt > 0.2:
            cv.circle((x + 24, y + 173), 8, (255, 70, 70))
            text(cv, (x + 44, y + 174), st, 26, anchor="lm", fill=(240, 240, 250))


def captions(cv, t):
    for (a, b, grp) in CHUNKS:
        if a - 0.02 <= t < b + 0.12:
            dt = t - a
            pop = ease_out_back(dt / 0.16)
            size = 66 * (0.75 + 0.25 * pop)
            parts = [w[2] for w in grp]
            total = tlen(cv, " ".join(parts), size, "mono")
            cy = 1250
            cv.rect((W / 2 - total / 2 - 26, cy - size * 0.72, W / 2 + total / 2 + 26, cy + size * 0.72),
                    (10, 10, 20, 200), radius=18)
            x = W / 2 - total / 2
            for (w0, w1, word, key) in grp:
                ww = tlen(cv, word, size, "mono")
                active = w0 <= t
                col = (255, 214, 64) if key else (255, 255, 255)
                if not active:
                    col = col[:3] + (120,)
                text(cv, (x + ww / 2, cy), word, size, fill=col, kind="mono")
                x += ww + tlen(cv, " ", size, "mono")
            return


def cta(cv, t):
    dt = t - SEGMENTS[-1]["start"]
    s = ease_out_back(dt / 0.4)
    cv.rect((60, 330, W - 60, 900), (8, 8, 20, int(200 * clamp(dt / 0.4))), radius=40)
    text(cv, (W / 2, 450), "WOULD YOU MISS", 84 * s, stroke=8)
    text(cv, (W / 2, 560), "THE MOON?", 110 * s, fill=(255, 214, 64), stroke=10)
    if dt > 0.9:
        s2 = ease_out_back((dt - 0.9) / 0.3)
        text(cv, (W / 2 - 150, 700), "YES", 80 * s2, fill=(90, 230, 120), stroke=8)
        text(cv, (W / 2, 700), "or", 50 * s2, stroke=6)
        text(cv, (W / 2 + 140, 700), "NO?", 80 * s2, fill=(255, 80, 80), stroke=8)
    if dt > 1.6:
        bounce = 8 * math.sin(dt * 6)
        text(cv, (W / 2, 800 + bounce), "COMMENT BELOW + TAG A FRIEND", 36, stroke=5)
        text(cv, (W / 2, 850 + bounce), "▼", 40, fill=(255, 214, 64))


def brand(cv):
    name = BRAND[0]
    x0 = W - 70 - max(tlen(cv, name, 36), tlen(cv, BRAND[1], 26))
    cv.d.arc(((x0 - 68) * SS, 80 * SS, (x0 - 14) * SS, 134 * SS), start=200, end=520,
             fill=(0, 200, 255), width=int(9 * SS))
    text(cv, (x0, 108), name, 36, anchor="lm", stroke=5)
    text(cv, (x0, 146), BRAND[1], 26, fill=(255, 214, 64), anchor="lm", stroke=4)


def tide_gauge(cv, t, dt):
    x0, y0 = 760, 1830
    s = clamp(dt / 0.4)
    cv.rect((x0 - 20, y0 - 380 * s, x0 + 290, y0 + 10), (10, 10, 24, 200), radius=20)
    if s < 1:
        return
    text(cv, (x0 + 135, y0 - 340), "TIDE HEIGHT", 28, stroke=0)
    full = 240
    now = full * (1 - (2 / 3) * ease((dt - 1.2) / 1.5))
    cv.rect((x0 + 30, y0 - 40 - full, x0 + 110, y0 - 40), (80, 110, 200), radius=10)
    cv.rect((x0 + 170, y0 - 40 - now, x0 + 250, y0 - 40), (90, 210, 255), radius=10)
    text(cv, (x0 + 70, y0 - 14), "BEFORE", 22)
    text(cv, (x0 + 210, y0 - 14), "NOW", 22, fill=(255, 214, 64))
    if dt > 2.8:
        text(cv, (x0 + 210, y0 - 60 - now), "~1/3", 34, fill=(255, 214, 64), stroke=5)


def turtles(cv, t, dt):
    rnd = random.Random(21)
    for i in range(14):
        sx, sy = rnd.uniform(80, 700), rnd.uniform(1430, 1600)
        head = rnd.uniform(0, 2 * math.pi)
        wig = math.sin(t * 2 + i) * 0.9
        h = head + wig
        sp = 22 * max(0.0, dt - rnd.uniform(0, 1.5))
        x, y = sx + math.cos(h) * sp, sy + math.sin(h) * sp * 0.5
        hx, hy = x + math.cos(h) * 20, y + math.sin(h) * 10
        cv.circle((hx, hy), 7, (120, 160, 90))
        for fa in (0.9, -0.9, 2.4, -2.4):
            fl = math.sin(t * 12 + i + fa) * 0.3
            cv.circle((x + math.cos(h + fa + fl) * 16, y + math.sin(h + fa + fl) * 9), 5, (110, 150, 80))
        cv.ellipse((x - 16, y - 10, x + 16, y + 10), fill=(90, 120, 60))
        cv.ellipse((x - 10, y - 6, x + 10, y + 6), fill=(130, 160, 80))
        if i % 3 == 0:
            text(cv, (x, y - 30), "?", 30, fill=(255, 214, 64), stroke=4)
    # owl on the lighthouse rail
    ox, oy = 990, 900
    cv.ellipse((ox - 26, oy - 40, ox + 26, oy + 30), fill=(120, 90, 60))
    cv.circle((ox - 10, oy - 18), 9, (255, 220, 120))
    cv.circle((ox + 10, oy - 18), 9, (255, 220, 120))
    look = math.sin(t * 2) * 3
    cv.circle((ox - 10 + look, oy - 18), 4, INK)
    cv.circle((ox + 10 + look, oy - 18), 4, INK)
    cv.poly([(ox - 4, oy - 8), (ox + 4, oy - 8), (ox, oy)], (255, 170, 60))
    text(cv, (ox, oy - 70), "?", 40, fill=(255, 214, 64), stroke=5)


def seasons(img, t, dt):
    left = Image.new("RGB", img.size)
    right = Image.new("RGB", img.size)
    world(left, t, dict(stars=160, tint=((200, 230, 255), 0.45)))
    world(right, t, dict(stars=60, tint=((255, 120, 40), 0.42)))
    cl = dg.Cv(left)
    rnd = random.Random(3)
    for i in range(160):  # blizzard
        x = (rnd.uniform(0, 560) + t * rnd.uniform(60, 160)) % 560
        y = (rnd.uniform(0, H) + t * rnd.uniform(200, 420)) % H
        cl.circle((x, y), rnd.uniform(3, 7), (255, 255, 255, 220))
    cl.poly([(0, 1380), (560, 1360), (560, 1660), (0, 1680)], (235, 245, 255, 200))
    cr = dg.Cv(right)
    cr.circle((860, 380), 140, (255, 200, 80))
    cr.circle((860, 380), 190, (255, 170, 60, 70))
    for i in range(8):  # heat shimmer
        y = 1000 + i * 70
        cr.line([(560 + k * 30, y + 8 * math.sin(t * 6 + k + i)) for k in range(18)], (255, 230, 180, 70), 4)
    mask = Image.new("L", img.size, 0)
    md = ImageDraw.Draw(mask)
    zig = [(540 + 30 * ((i % 2) * 2 - 1), i * 120) for i in range(0, 18)]
    md.polygon([(p[0] * SS, p[1] * SS) for p in zig] + [(W * SS, H * SS), (W * SS, 0)], fill=255)
    out = Image.composite(right, left, mask)
    img.paste(out)
    cv = dg.Cv(img)
    text(cv, (270, 560), "BRUTAL WINTERS", 46, fill=(200, 235, 255), stroke=6)
    text(cv, (810, 620), "SCORCHING SUMMERS", 42, fill=(255, 200, 120), stroke=6)
    return cv


# ------------------------------------------------------------------ frame

def draw_scene(img, t, i):
    seg = SEGMENTS[i]
    dt = t - seg["start"]
    name = seg["scene"]
    if name == "hook":
        v = 1 - ease((t - VANISH_T) / 0.35)
        cv = world(img, t, dict(moon=v, moon_r=110 * (1 + 0.15 * max(0, v - 0.8))))
        if VANISH_T <= t < VANISH_T + 0.9:  # poof
            u = (t - VANISH_T) / 0.9
            for k in range(18):
                a = k / 18 * 2 * math.pi
                r = 60 + 260 * u
                cv.circle((760 + math.cos(a) * r, 400 + math.sin(a) * r), 10 * (1 - u) + 1, (255, 255, 255, int(255 * (1 - u))))
        kai(img, t, "point_up")
    elif name == "empty":
        cv = world(img, t, dict())
        cv.ellipse((650, 290, 870, 510), outline=(255, 255, 255, int(90 + 60 * math.sin(t * 4))), width=4)
        text(cv, (760, 400), "?", 90, fill=(255, 255, 255, 160))
        kai(img, t, "shrug")
    elif name == "tides":
        amp = 55 - 37 * ease((dt - 1.2) / 1.5)
        cv = world(img, t, dict(tide_amp=amp, tide_speed=0.45))
        # sun pulling from the left edge
        cv.circle((-30, HORIZON - 40), 110, (255, 200, 90, 200))
        for k in range(3):
            x = 120 + k * 90 + (t * 60) % 90
            cv.line([(x + 40, HORIZON + 140), (x, HORIZON + 140)], (255, 210, 110, 200), 8)
            cv.poly([(x - 10, HORIZON + 140), (x + 14, HORIZON + 126), (x + 14, HORIZON + 154)], (255, 210, 110, 200))
        kai(img, t, "think")
        tide_gauge(cv, t, dt)
    elif name == "dark":
        d = ease(dt / 1.4)
        cv = world(img, t, dict(dark=d, stars=160 + 900 * d))
        kai(img, t, "flashlight", flashlight=True)
    elif name == "animals":
        cv = world(img, t, dict(dark=0.6, stars=600))
        turtles(cv, t, dt)
        kai(img, t, "scratch")
    elif name == "wobble":
        cv = space(img, t, tilt_wobble=ease((dt - 0.5) / 2.5))
        kai(img, t, "float", origin=(250, 1800), k=0.6)
    elif name == "seasons":
        cv = seasons(img, t, dt)
        kai(img, t, "shiver", origin=(540, 1800), k=0.66, jitter=3)
    elif name == "stabilizer":
        cv = space(img, t, tilt_wobble=0.0, show_moon=True, glow=ease(dt / 1.0))
        text(cv, (540, 330), "THE MOON = EARTH'S STABILIZER", 44, fill=(255, 214, 64), stroke=6)
        kai(img, t, "thumbs", origin=(250, 1800), k=0.6)
    else:  # cta
        cv = world(img, t, dict(moon=ease(dt / 0.8), stars=300))
        kai(img, t, "wave")
    cv = dg.Cv(img)
    counter_hud(cv, t, i)
    if name == "cta":
        cta(cv, t)
    return cv


def frame(t):
    i = scene_at(t)
    img = Image.new("RGB", (W * SS, H * SS))
    draw_scene(img, t, i)
    # crossfade into each new scene (except the space cut-ins, which cut on a flash)
    seg = SEGMENTS[i]
    if i > 0 and t - seg["start"] < 0.3:
        prev = Image.new("RGB", (W * SS, H * SS))
        draw_scene(prev, t, i - 1)
        img = Image.blend(prev, img, ease((t - seg["start"]) / 0.3))
    cv = dg.Cv(img)
    captions(cv, t)
    brand(cv)
    cv.rect((0, H - 10, W * t / TOTAL, H), (255, 214, 64))
    out = img.reduce(SS)
    if t < 0.2:
        out = Image.blend(Image.new("RGB", out.size), out, t / 0.2)
    if t > TOTAL - 0.5:
        out = Image.blend(out, Image.new("RGB", out.size), (t - (TOTAL - 0.5)) / 0.5)
    return out


def render_range(args):
    a, b, path = args
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(a, b):
        proc.stdin.write(frame(i / FPS).tobytes())
    proc.stdin.close()
    proc.wait()
    return path


# ------------------------------------------------------------------ audio

def soundtrack(narration: Path, out: Path, work: Path):
    SR = dg.SR
    raw = work / "nar.raw"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(narration), "-ac", "1", "-ar", str(SR),
                    "-f", "s16le", str(raw)], check=True)
    voice = np.frombuffer(raw.read_bytes(), dtype=np.int16).astype(np.float64) / 32768
    n = int(TOTAL * SR)
    music = np.zeros(n)

    def add(sig, t0, vol=1.0):
        i = int(t0 * SR)
        j = min(n, i + len(sig))
        if i < n:
            music[i:j] += sig[:j - i] * vol

    # mysterious-to-hopeful pad: Am - F - C - G, one chord per 4 s, soft pulse
    prog = [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)]
    for k in range(int(TOTAL / 4) + 1):
        notes = prog[k % 4]
        sig = sum(dg.osc(dg.mtof(m), 4.2, "saw", detune=0.01) for m in notes)
        e = np.minimum(1, np.arange(len(sig)) / SR / 0.8) * np.minimum(1, (len(sig) - np.arange(len(sig))) / SR / 0.8)
        add(sig * e * 0.018, k * 4)
        add(dg.osc(dg.mtof(notes[0] - 24), 4.0, "sine") * e[:int(4.0 * SR)] * 0.10, k * 4)
    # soft ticking pulse (time passing), 2 per second
    for k in range(int(TOTAL * 2)):
        tt = k / 2
        if tt < 0.5:
            continue
        click = dg.osc(2400, 0.03, "sine") * dg.env(int(0.03 * SR), 0.001, 0.008)
        add(click, tt, 0.05)
    # SFX: moon "poof", scene whooshes, wobble boom, CTA sparkle
    add(np.random.default_rng(1).uniform(-1, 1, int(0.6 * SR)) * dg.env(int(0.6 * SR), 0.005, 0.15), VANISH_T, 0.25)
    for k, m in enumerate((84, 79, 76, 72)):
        add(dg.lead_note(m, 0.25), VANISH_T + 0.05 + k * 0.07, 1.2)
    for seg in SEGMENTS[1:]:
        nn = int(0.5 * SR)
        sw = np.random.default_rng(2).uniform(-1, 1, nn)
        sw = np.diff(sw, prepend=0) * np.sin(np.linspace(0, np.pi, nn))
        add(sw, seg["start"] - 0.15, 0.12)
    wob = SEGMENTS[5]["start"]
    add(dg.kick() * 1.2, wob + 0.3)
    add(dg.osc(45, 2.0, "sine") * dg.env(int(2.0 * SR), 0.01, 0.8), wob + 0.3, 0.35)
    for k, m in enumerate((72, 76, 79, 84)):
        add(dg.lead_note(m, 0.3), SEGMENTS[-1]["start"] + 0.1 + k * 0.1, 1.0)

    v = np.zeros(n)
    v[:min(n, len(voice))] = voice[:n]
    envv = np.convolve(np.abs(v), np.ones(2205) / 2205, mode="same")
    duck = 1 - 0.5 * np.clip(envv * 10, 0, 1)
    mixd = np.tanh((music * duck) * 1.4) * 0.6 + v * 1.0
    mixd /= np.abs(mixd).max() + 1e-9
    mixd *= 0.9
    fo = int(0.5 * SR)
    mixd[-fo:] *= np.linspace(1, 0, fo)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mixd * 32767).astype(np.int16).tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--narration", required=True, help="narration audio (the timings above match it)")
    ap.add_argument("-o", "--output", default="what-if-moon.mp4")
    ap.add_argument("--still", type=float)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.still is not None:
        frame(args.still).save(out.with_suffix(".png"))
        print(out.with_suffix(".png"))
        return 0
    work = Path(tempfile.mkdtemp(prefix="whatif-"))
    try:
        wav = work / "mix.wav"
        soundtrack(Path(args.narration), wav, work)
        n = int(TOTAL * FPS)
        k = max(1, args.workers)
        bounds = [(n * i // k, n * (i + 1) // k, work / f"p{i}.mp4") for i in range(k)]
        with mp.get_context("fork").Pool(k) as pool:
            parts = pool.map(render_range, bounds)
        (work / "l.txt").write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(work / "l.txt"),
                        "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-t", f"{TOTAL:.3f}", "-movflags", "+faststart", str(out)], check=True)
        frame(1.0).save(out.with_name(out.stem + "_cover.png"))
        print(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Arc Lab Dance Party: a long-form (about 4 min, 16:9) interactive dance-workout video.

The format: footprint, hand, jump and clap tiles slide down a perspective lane track
toward a hit line in time with the music, while a crew of original Arc Lab characters
dances each move on both sides. Viewers copy the moves. There's a combo counter, hit
effects, section banners and a results screen.

Everything is generated:
  * every frame is drawn in code (Pillow), using the Arc Lab character rig from animate_arc.py
  * the song is an original 120 BPM track synthesized with numpy
  * coach voice clips come from --voice-dir (e.g. ElevenLabs takes split into 01.wav,
    02.wav, ... in LINES order), with a local espeak-ng fallback

Usage
  python3 dance_game.py -o dance-party.mp4 [--voice-dir voice/] [--workers 4]
  python3 dance_game.py --still 40 -o check.png
  python3 dance_game.py --song-only -o song.wav
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
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import animate_arc as rig  # noqa: E402

W, H = 1920, 1080
SS = 2
FPS = 30
BPM = 120
BEAT = 60 / BPM
BAR = 4 * BEAT
LOOK = 2.4            # seconds a tile takes to travel from the far end to the hit line
INK = (17, 16, 28)

# --------------------------------------------------------------- structure
INTRO_END = 6.0
GAME_START = 10.0
SECTIONS = [  # name, banner, start, bars
    ("warm", "WARM-UP: STEPS", 10.0, 16),
    ("hands", "ADD YOUR HANDS", 46.0, 16),
    ("jump", "JUMP + CLAP", 82.0, 16),
    ("speed", "SPEED UP!", 118.0, 16),
    ("arms", "ARMS UP!", 154.0, 16),
    ("final", "FINAL ROUND!", 190.0, 16),
]
OUTRO = 222.0
TOTAL = 232.0

LINES = [
    "Hey hey! Welcome to the Arc Lab Dance Party!",          # 1
    "Get ready to move!",                                    # 2
    "Three!", "Two!", "One!",                                # 3-5
    "Let's go!",                                             # 6
    "Step left, step right! Follow the tiles!",              # 7
    "Awesome! Now add your hands!",                          # 8
    "Punch it out! Left! Right!",                            # 9
    "Jump time! Get those knees up!",                        # 10
    "Clap clap! You're on fire!",                            # 11
    "Speed it up! Faster feet!",                             # 12
    "Halfway there! Keep going!",                            # 13
    "Arms up high! Big circles!",                            # 14
    "Last round! Give it everything you've got!",            # 15
    "Woohoo! Workout complete! You crushed it!",             # 16
    "Follow Arc Lab for more dance games! See you next time!",  # 17
    "Left!", "Right!", "Jump!", "Clap!",                     # 18-21
    "Move, move!", "Arc Lab groove!", "Perfect!",            # 22-24
]

# tile kinds -> lanes (0 left hand, 1 left foot, 2 right foot, 3 right hand)
LANES = {"stepL": [1], "stepR": [2], "punchL": [0], "punchR": [3],
         "jump": [1, 2], "clap": [0, 1, 2, 3], "arms": [0, 3]}
YELLOW, BLUE, PINK, GREEN = (255, 205, 40), (60, 150, 255), (255, 80, 170), (90, 230, 120)


def build_chart():
    """List of (time, kind). Deterministic."""
    notes = []
    rnd = random.Random(42)
    for name, _, start, bars in SECTIONS:
        first = start + 2 * BAR          # 2 bars to let the first tiles travel in
        nbeats = (bars - 2) * 4
        for b in range(nbeats):
            t = first + b * BEAT
            bar, beat = divmod(b, 4)
            if name == "warm":
                notes.append((t, "stepL" if b % 2 == 0 else "stepR"))
            elif name == "hands":
                notes.append((t, ["stepL", "stepR", "punchL", "punchR"][beat]))
            elif name == "jump":
                if beat == 0:
                    notes.append((t, "jump"))
                elif beat == 2:
                    notes.append((t, "clap"))
                else:
                    notes.append((t, "stepL" if beat == 1 else "stepR"))
            elif name == "speed":
                if bar % 4 < 2:   # eighth-note steps
                    notes.append((t, "stepL"))
                    notes.append((t + BEAT / 2, "stepR"))
                else:
                    notes.append((t, ["punchL", "punchR", "punchL", "jump"][beat]))
            elif name == "arms":
                notes.append((t, ["arms", "stepL", "arms", "stepR"][beat] if bar % 2 == 0
                              else ["punchL", "punchR", "clap", "jump"][beat]))
            elif name == "final":
                pat = [["stepL", "stepR", "punchL", "punchR"], ["jump", "clap", "jump", "clap"],
                       ["arms", "stepL", "stepR", "arms"], ["punchL", "punchR", "stepL", "stepR"]]
                notes.append((t, pat[bar % 4][beat]))
                if bar % 4 == 3 and beat in (1, 3):
                    notes.append((t + BEAT / 2, rnd.choice(["stepL", "stepR"])))
    notes.sort()
    return notes


CHART = build_chart()
CHART_T = np.array([n[0] for n in CHART])


def voice_schedule():
    """(time, line_number) pairs, 1-based like LINES numbering."""
    ev = [(0.6, 1), (3.6, 2), (6.0, 3), (7.0, 4), (8.0, 5), (9.0, 6), (10.6, 7),
          (SECTIONS[1][2] - 3.6, 8), (SECTIONS[1][2] + 1.0, 9),
          (SECTIONS[2][2] - 3.6, 10), (SECTIONS[2][2] + 14.0, 11),
          (SECTIONS[3][2] - 3.6, 12), (SECTIONS[3][2] + 6.0, 13),
          (SECTIONS[4][2] - 3.6, 14), (SECTIONS[5][2] - 3.6, 15),
          (OUTRO + 0.4, 16), (OUTRO + 4.6, 17)]
    # on-beat callouts, a beat before the tile lands
    for i, (t, k) in enumerate(CHART):
        sec = section_at(t)
        if sec == "warm" and i % 8 == 0 and t < SECTIONS[0][2] + 20:
            ev.append((t - BEAT * 0.9, 18 if k == "stepL" else 19))
        if sec == "jump" and k == "jump" and i % 3 == 0:
            ev.append((t - BEAT * 0.9, 20))
        if sec == "jump" and k == "clap" and i % 5 == 0 and t > SECTIONS[2][2] + 20:
            ev.append((t - BEAT * 0.9, 21))
    # chant on the chorus halves of speed + final sections
    for name, _, start, bars in SECTIONS:
        if name in ("speed", "final", "arms"):
            for bar in (8, 12):
                ev.append((start + bar * BAR, 22))
                ev.append((start + bar * BAR + BAR / 2, 23))
    for t in (SECTIONS[1][2] + 20, SECTIONS[3][2] + 26, SECTIONS[5][2] + 24):
        ev.append((t, 24))
    return sorted(ev)


def section_at(t):
    for name, banner, start, bars in reversed(SECTIONS):
        if t >= start:
            return name if t < start + bars * BAR else "break"
    return "intro"


# --------------------------------------------------------------- canvas helpers

def font(size):
    path = rig.find_font(True)
    return ImageFont.truetype(path, int(size * SS)) if path else ImageFont.load_default(int(size * SS))


_F = {}


def F(size):
    size = max(6, int(size))
    if size not in _F:
        _F[size] = font(size)
    return _F[size]


class Cv:
    """Screen canvas, 1x coordinates."""

    def __init__(self, img):
        self.img = img
        self.d = ImageDraw.Draw(img, "RGBA")

    def poly(self, pts, fill, outline=None, width=0):
        self.d.polygon([(x * SS, y * SS) for x, y in pts], fill=fill,
                       outline=outline, width=int(width * SS) if outline else 0)

    def circle(self, c, r, fill, outline=None, width=0):
        x, y, r = c[0] * SS, c[1] * SS, r * SS
        self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=outline,
                       width=int(width * SS) if outline else 0)

    def ellipse(self, box, fill=None, outline=None, width=0):
        self.d.ellipse(tuple(v * SS for v in box), fill=fill, outline=outline, width=int(width * SS))

    def line(self, pts, fill, width):
        self.d.line([(x * SS, y * SS) for x, y in pts], fill=fill, width=int(width * SS), joint="curve")

    def rect(self, box, fill, radius=0, outline=None, width=0):
        self.d.rounded_rectangle(tuple(v * SS for v in box), radius=radius * SS, fill=fill,
                                 outline=outline, width=int(width * SS))

    def text(self, xy, s, size, fill=(255, 255, 255), anchor="mm", stroke=0, stroke_fill=INK):
        self.d.text((xy[0] * SS, xy[1] * SS), s, font=F(size), fill=fill, anchor=anchor,
                    stroke_width=int(stroke * SS), stroke_fill=stroke_fill)


class XCanvas(rig.Canvas):
    """Draws the rig's character (authored at ~700 px tall, ground y=rig.G) scaled/mirrored."""

    def __init__(self, img, origin, k, mirror=False):
        super().__init__(img)
        self.o, self.k, self.sx = origin, k, (-k if mirror else k)

    def P(self, p):
        return ((self.o[0] + self.sx * (p[0] - 540)) * SS, (self.o[1] + self.k * (p[1] - rig.G)) * SS)

    def poly(self, pts, fill, outline=INK, width=rig.OL):
        pts = [self.P(p) for p in pts]
        if outline is not None and width:
            self.d.polygon(pts, fill=fill, outline=outline, width=max(1, int(width * self.k * SS)))
        else:
            self.d.polygon(pts, fill=fill)

    def circle(self, c, r, fill, outline=INK, width=rig.OL):
        x, y = self.P(c)
        r *= self.k * SS
        if outline is not None and width:
            w = width * self.k * SS
            self.d.ellipse((x - r - w, y - r - w, x + r + w, y + r + w), fill=outline)
        self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def _capsule(self, a, b, r, fill):
        A, B = self.P(a), self.P(b)
        rr = r * self.k * SS
        self.d.line([A, B], fill=fill, width=max(1, int(2 * rr)))
        for (x, y) in (A, B):
            self.d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill)

    def polyline(self, pts, w, fill):
        pts = [self.P(p) for p in pts]
        self.d.line(pts, fill=fill, width=max(1, int(w * self.k * SS)), joint="curve")
        r = w * self.k * SS / 2
        for (x, y) in (pts[0], pts[-1]):
            self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


# --------------------------------------------------------------- dancers

CREW = [  # name, colors, screen feet origin, scale, mirrored
    ("KAI", {"hoodie": "#FF7A1A", "pants": "#2B2D42", "skin": "#F4C7A1", "hair": "#22204A",
             "band": "#E8283C", "shoe": "#F5F5F5", "aura": "#3DF5FF", "accent": "#FFD23F"},
     (330, 985), 0.60, False),
    ("MIRA", {"hoodie": "#9B5CFF", "pants": "#30244F", "skin": "#C98E66", "hair": "#121019",
              "band": "#FFD23F", "shoe": "#FFFFFF", "aura": "#FF7BD5", "accent": "#FFD23F"},
     (545, 800), 0.44, False),
    ("BOLT", {"hoodie": "#2ECC71", "pants": "#1F3B4D", "skin": "#8D5A3B", "hair": "#0E0E14",
              "band": "#3D9BFF", "shoe": "#FFFFFF", "aura": "#B6FF5C", "accent": "#FFD23F"},
     (1590, 985), 0.60, True),
    ("JUNO", {"hoodie": "#FF4F9A", "pants": "#3A2140", "skin": "#F1C9A8", "hair": "#E86A1C",
              "band": "#20D6C7", "shoe": "#FFFFFF", "aura": "#FFE45C", "accent": "#FFD23F"},
     (1375, 800), 0.44, True),
]


def current_move(t):
    i = int(np.searchsorted(CHART_T, t, side="right")) - 1
    if i < 0:
        return None, 0.0
    T, k = CHART[i]
    if t - T > 0.46:
        return None, 0.0
    # simultaneous punches read as "arms"
    return k, (t - T) / 0.46


def dance_pose(t, move, ph, offset):
    G = rig.G
    beat_ph = ((t + offset) / BEAT) % 1.0
    bob = 12 * abs(math.sin(math.pi * beat_ph))
    e = math.sin(math.pi * clamp(ph))
    p = rig.Pose(hip=(540, G - 214 + bob), lean=4, tilt=-6, eye=1.0, brow=0.3, mouth="smile",
                 wind=0.6, fist=True)
    if int((t + offset * 7) * 10) % 37 == 0:
        p.eye = 0.1
    sh = rig.shoulder_of(p)
    hands = [(sh[0] + 46 + 14 * math.sin(2 * math.pi * beat_ph), sh[1] + 130),
             (sh[0] - 24, sh[1] + 134)]
    feet = [(566, G - 14), (516, G - 14)]
    if move in ("stepL", "stepR"):
        i = 0 if move == "stepL" else 1
        x0 = feet[i][0]
        feet[i] = (x0 + (95 if i == 0 else -80) * e, G - 14 - 46 * max(0.0, math.sin(2 * math.pi * clamp(ph))))
        p.hip = (540 + (18 if i == 0 else -14) * e, p.hip[1] + 16 * e)
        sh = rig.shoulder_of(p)
        hands = [(sh[0] + 46 - 60 * e * (1 if i == 0 else -1), sh[1] + 120),
                 (sh[0] - 24 + 50 * e * (1 if i == 0 else -1), sh[1] + 124)]
        p.mouth = "grit" if e > 0.6 else "smile"
    elif move in ("punchL", "punchR"):
        i = 0 if move == "punchL" else 1
        hands[i] = (sh[0] + rig.lerp(50, 172, e), sh[1] + rig.lerp(90, 8, e))
        hands[1 - i] = (sh[0] + 52, sh[1] + 40)
        p.lean = 4 + 10 * e
        p.mouth, p.brow = "grit", 1.0
    elif move == "jump":
        p.hip = (540, G - 214 - 150 * e)
        feet = [(574, p.hip[1] + 196 - 70 * e), (522, p.hip[1] + 196 - 70 * e)]
        sh = rig.shoulder_of(p)
        hands = [(sh[0] + 50, sh[1] - 40 - 120 * e), (sh[0] - 10, sh[1] - 40 - 120 * e)]
        p.mouth = "open"
    elif move == "clap":
        s = abs(math.cos(math.pi * clamp(ph) * 2))
        hands = [(sh[0] + 118, sh[1] - 70), (sh[0] + 118 - 70 * s, sh[1] - 66)]
        p.mouth = "open"
    elif move == "arms":
        a = 2 * math.pi * clamp(ph)
        hands = [(sh[0] + 70 + 70 * math.cos(a), sh[1] - 150 + 50 * math.sin(a)),
                 (sh[0] - 50 - 70 * math.cos(a), sh[1] - 150 - 50 * math.sin(a))]
        p.mouth = "open"
    p.hands, p.feet = hands, feet
    return p


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease_out_back(t, s=1.7):
    t = clamp(t) - 1
    return t * t * ((s + 1) * t + s) + 1


# --------------------------------------------------------------- highway

VX, VY, HIT_Y, LANE_W = 960, 300, 930, 150
PERSP = 2.6


def proj(u, z):
    """Lane coordinate u in [0,4], depth z (0 = hit line, 1 = LOOK seconds away)."""
    p = 1.0 / (1.0 + PERSP * z)
    return (VX + (u - 2) * LANE_W * p, VY + (HIT_Y - VY) * p)


def quad(u0, u1, z0, z1):
    return [proj(u0, z0), proj(u1, z0), proj(u1, z1), proj(u0, z1)]


def bilinear(q, s, r):
    """Map unit-square (s across, r along depth; r=0 near edge) onto quad q."""
    a, b, c, d = q  # near-left, near-right, far-right, far-left
    nx, ny = a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s
    fx, fy = d[0] + (c[0] - d[0]) * s, d[1] + (c[1] - d[1]) * s
    return (nx + (fx - nx) * r, ny + (fy - ny) * r)


def circle_pts(cx, cy, rx, ry, n=14):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def foot_icon(mirror):
    sole = [(0.5 + (x - 0.5) * (-1 if mirror else 1), y) for x, y in circle_pts(0.5, 0.42, 0.17, 0.27)]
    toes = []
    for i, (tx, ty, tr) in enumerate(((0.38, 0.80, 0.07), (0.5, 0.84, 0.06), (0.6, 0.82, 0.055),
                                      (0.69, 0.77, 0.05), (0.76, 0.70, 0.045))):
        if mirror:
            tx = 1 - tx
        toes.append(circle_pts(tx, ty, tr, tr, 8))
    return [sole] + toes


def hand_icon(mirror):
    palm = circle_pts(0.5, 0.38, 0.2, 0.2)
    parts = [palm]
    for i, fx in enumerate((0.36, 0.46, 0.56, 0.66)):
        fx2 = 1 - fx if mirror else fx
        h = (0.30, 0.36, 0.34, 0.27)[i]
        parts.append([(fx2 - 0.045, 0.5), (fx2 + 0.045, 0.5), (fx2 + 0.045, 0.5 + h), (fx2 - 0.045, 0.5 + h)])
    tx = 0.78 if not mirror else 0.22
    parts.append(circle_pts(tx, 0.45, 0.06, 0.12, 10))
    return parts


ICONS = {"stepL": foot_icon(False), "stepR": foot_icon(True), "punchL": hand_icon(False),
         "punchR": hand_icon(True)}
CHEVRON = [[(0.25, 0.25), (0.5, 0.55), (0.75, 0.25), (0.75, 0.42), (0.5, 0.72), (0.25, 0.42)],
           [(0.25, 0.55), (0.5, 0.85), (0.75, 0.55), (0.75, 0.70), (0.5, 0.98), (0.25, 0.70)]]


def tile_color(kind):
    return {"stepL": YELLOW, "punchL": YELLOW, "stepR": BLUE, "punchR": BLUE,
            "jump": GREEN, "clap": PINK, "arms": (255, 140, 40)}[kind]


def draw_highway(cv, t):
    # road body
    cv.poly(quad(0, 4, -0.25, 1.25), (20, 18, 40, 215))
    pulse = 0.5 + 0.5 * math.cos(2 * math.pi * (t / BEAT))
    for u in range(5):
        a = proj(u, -0.25)
        b = proj(u, 1.25)
        col = (255, 255, 255, 60) if u in (1, 2, 3) else (120 + int(100 * pulse), 220, 255, 230)
        cv.line([a, b], col, 3 if u in (1, 2, 3) else 7)
    # moving rungs
    for k in range(12):
        z = ((k * 0.125) - (t / LOOK) % 0.125)
        if -0.2 < z < 1.25:
            cv.line([proj(0, z), proj(4, z)], (255, 255, 255, int(40 * (1 - z / 1.3))), 2)
    # hit zone squares
    for lane in range(4):
        q = quad(lane + 0.08, lane + 0.92, -0.06, 0.06)
        col = YELLOW if lane < 2 else BLUE
        cv.poly(q, col + (40,), outline=col + (230,), width=4)


def draw_tile(cv, kind, z, lane_list, alpha=255):
    col = tile_color(kind)
    u0, u1 = min(lane_list), max(lane_list) + 1
    if kind == "arms":   # two separate hand tiles
        for ln, k2 in ((0, "punchL"), (3, "punchR")):
            draw_tile(cv, k2, z, [ln], alpha)
        return
    q = quad(u0 + 0.08, u1 - 0.08, z - 0.06, z + 0.06)
    cv.poly(q, col + (alpha,), outline=(255, 255, 255, alpha), width=3)
    # 3D lip on the near edge
    lip = [q[0], q[1], (q[1][0], q[1][1] + 10 / (1 + PERSP * max(z, 0))), (q[0][0], q[0][1] + 10 / (1 + PERSP * max(z, 0)))]
    cv.poly(lip, rig.shade(col, 0.6) + (alpha,))
    icon = ICONS.get(kind)
    if kind == "jump":
        icon = CHEVRON
    if kind == "clap":
        # four little stars across the bar
        for ln in range(4):
            qq = quad(ln + 0.2, ln + 0.8, z - 0.06, z + 0.06)
            star = []
            for i in range(10):
                r = 0.42 if i % 2 == 0 else 0.18
                a = -math.pi / 2 + i * math.pi / 5
                star.append(bilinear(qq, 0.5 + r * math.cos(a), 0.5 - r * math.sin(a)))
            cv.poly(star, (255, 255, 255, alpha))
        return
    if icon:
        for part in icon:
            cv.poly([bilinear(q, s, 1 - r) for s, r in part], (30, 24, 50, alpha))


def hit_fx(cv, t):
    """Rings + sparkles at the hit line for tiles hit within the last 0.5 s."""
    lo = int(np.searchsorted(CHART_T, t - 0.5))
    hi = int(np.searchsorted(CHART_T, t, side="right"))
    for i in range(lo, hi):
        T, kind = CHART[i]
        dt = t - T
        for lane in LANES[kind]:
            c = proj(lane + 0.5, 0)
            r = 40 + 260 * dt
            a = int(255 * (1 - dt / 0.5))
            col = tile_color(kind if kind != "arms" else ("punchL" if lane == 0 else "punchR"))
            cv.ellipse((c[0] - r, c[1] - r * 0.32, c[0] + r, c[1] + r * 0.32), outline=col + (a,), width=6)
            rnd = random.Random(i * 7 + lane)
            for _ in range(8):
                ang = rnd.uniform(math.pi, 2 * math.pi)
                sp = rnd.uniform(200, 520)
                pp = (c[0] + math.cos(ang) * sp * dt, c[1] + math.sin(ang) * sp * dt * 0.8 + 300 * dt * dt)
                cv.circle(pp, 7 * (1 - dt / 0.5) + 1, (255, 255, 255, a))


JUDGE = ["PERFECT!", "GREAT!", "PERFECT!", "AWESOME!", "PERFECT!", "NICE!"]


def judgement(cv, t):
    i = int(np.searchsorted(CHART_T, t, side="right")) - 1
    if i < 0:
        return
    T, kind = CHART[i]
    dt = t - T
    if dt > 0.4 or i % 2:
        return
    s = ease_out_back(dt / 0.15)
    txt = JUDGE[(i // 2) % len(JUDGE)]
    cv.text((VX, HIT_Y - 150 - 60 * dt), txt, 64 * s, fill=(255, 240, 120), stroke=8)


def combo(cv, t):
    n = int(np.searchsorted(CHART_T, t, side="right"))
    if n == 0 or t > OUTRO:
        return
    dt = t - CHART[n - 1][0]
    pop = 1 + 0.18 * max(0.0, 1 - dt / 0.15)
    cv.text((1500, 120), str(n), 120 * pop, stroke=10)
    cv.text((1500, 200), "COMBO", 34, stroke=6)


# --------------------------------------------------------------- background

def make_background():
    img = Image.new("RGB", (W * SS, H * SS))
    top, mid = np.array((112, 210, 255)), np.array((205, 240, 255))
    ys = np.linspace(0, 1, 560 * SS)[:, None]
    sky = (top * (1 - ys) + mid * ys).astype(np.uint8)
    img.paste(Image.fromarray(np.repeat(sky[:, None, :], W * SS, axis=1), "RGB"), (0, 0))
    cv = Cv(img)
    # clouds
    rnd = random.Random(3)
    for cx, cy, s in ((240, 140, 1.1), (700, 90, 0.8), (1250, 150, 1.0), (1700, 80, 0.9), (1500, 300, 0.6),
                      (420, 330, 0.7)):
        for dx, dy, r in ((0, 0, 70), (70, 15, 55), (-70, 18, 52), (25, -38, 52), (-30, -25, 45)):
            cv.circle((cx + dx * s, cy + dy * s), r * s, (255, 255, 255, 245))
    # distant colorful blocks (toy-town skyline)
    palette = [(255, 170, 90), (130, 200, 120), (250, 120, 150), (150, 140, 240), (255, 215, 90)]
    x = -40
    while x < W:
        w = rnd.randint(80, 170)
        h = rnd.randint(90, 230)
        c = palette[rnd.randrange(len(palette))]
        cv.rect((x, 560 - h, x + w, 562), c, radius=14)
        for wy in range(560 - h + 25, 540, 40):
            for wx in range(x + 18, x + w - 24, 34):
                cv.rect((wx, wy, wx + 16, wy + 20), (255, 255, 255, 120), radius=4)
        x += w + rnd.randint(6, 26)
    # stage floor in perspective
    floor_top = 560
    ys = np.linspace(0, 1, (H - floor_top) * SS)[:, None]
    fl = (np.array((255, 214, 150)) * (1 - ys) + np.array((240, 170, 110)) * ys).astype(np.uint8)
    img.paste(Image.fromarray(np.repeat(fl[:, None, :], W * SS, axis=1), "RGB"), (0, floor_top * SS))
    cv = Cv(img)
    for i in range(-14, 15):  # boards converging to the vanishing point
        bx = VX + i * 180
        cv.line([(VX + i * 18, floor_top), (bx, H)], (200, 130, 80, 110), 3)
    for k in range(1, 9):
        y = floor_top + (H - floor_top) * (k / 9) ** 1.6
        cv.line([(0, y), (W, y)], (200, 130, 80, 60), 2)
    cv.line([(0, floor_top), (W, floor_top)], (180, 110, 70), 4)
    return img


def draw_arch(cv, t):
    pulse = 0.5 + 0.5 * math.cos(2 * math.pi * t / BEAT)
    c = (VX, 395)
    for r, w, a in ((150, 26, 60), (130, 14, 255)):
        col = (60 + int(150 * pulse), 230, 255, a)
        cv.d.arc(((c[0] - r) * SS, (c[1] - r) * SS, (c[0] + r) * SS, (c[1] + r) * SS),
                 start=200, end=520, fill=col, width=int(w * SS))
    cv.text((VX, 395), "ARC", 64, fill=(255, 255, 255), stroke=6)


def stage_lights(cv, t):
    for side in (-1, 1):
        for k in range(2):
            a = math.sin(t * 0.9 + k * 1.7 + side) * 0.35
            ox = VX + side * (820 - k * 120)
            pts = [(ox, -20), (ox + math.sin(a - 0.07) * 1400, 1100), (ox + math.sin(a + 0.07) * 1400, 1100)]
            col = [(255, 120, 220, 22), (120, 220, 255, 22)][k]
            cv.poly(pts, col)


# --------------------------------------------------------------- HUD

def banner(cv, t):
    for name, text, start, bars in SECTIONS:
        dt = t - start
        if 0 <= dt < 2.6:
            s = ease_out_back(dt / 0.3) if dt < 2.3 else max(0.0, 1 - (dt - 2.3) / 0.3)
            w = 420 + 12 * len(text)
            cv.rect((VX - w / 2 * s, 560 - 50 * s, VX + w / 2 * s, 560 + 50 * s), (255, 60, 140, 235),
                    radius=50, outline=(255, 255, 255), width=5)
            cv.text((VX, 560), text, 56 * s, stroke=0)
    # next-move preview during the breaks between sections
    for i, (name, text, start, bars) in enumerate(SECTIONS[1:], 1):
        if start - 4 <= t < start:
            cv.text((VX, 600), "NEXT", 40, fill=(255, 255, 255), stroke=6)
            cv.text((VX, 660), text, 64, fill=(255, 230, 90), stroke=8)


def hud(cv, t):
    # logo
    cv.d.arc(((40) * SS, 30 * SS, 110 * SS, 100 * SS), start=200, end=520, fill=(0, 200, 255), width=int(12 * SS))
    cv.text((130, 66), "ARC LAB", 44, anchor="lm", stroke=6)
    cv.text((132, 112), "DANCE PARTY", 28, fill=(255, 230, 90), anchor="lm", stroke=5)
    # timer
    left = max(0, OUTRO - max(t, GAME_START))
    m, s = divmod(int(left + 0.999), 60)
    if GAME_START <= t < OUTRO:
        cv.rect((40, 150, 230, 210), (20, 18, 40, 200), radius=30)
        cv.text((135, 180), f"{m}:{s:02d}", 40)
    # progress bar
    cv.rect((0, H - 12, W * t / TOTAL, H), (255, 60, 140))


def intro(cv, t):
    cv.rect((0, 0, W, H), (8, 8, 14))
    rnd = random.Random(int(t * 20))
    for i in range(70):  # speed lines
        a = rnd.uniform(0, 2 * math.pi)
        r0 = rnd.uniform(260, 420)
        r1 = rnd.uniform(1100, 1500)
        w = rnd.uniform(0.004, 0.012)
        c = (VX, H / 2)
        cv.poly([(c[0] + math.cos(a) * r0, c[1] + math.sin(a) * r0),
                 (c[0] + math.cos(a - w) * r1, c[1] + math.sin(a - w) * r1),
                 (c[0] + math.cos(a + w) * r1, c[1] + math.sin(a + w) * r1)], (255, 255, 255, 220))
    s = ease_out_back((t - 0.3) / 0.4)
    if t > 0.3:
        cv.text((VX, 420), "NEW", 120 * s, fill=(255, 230, 90), stroke=12)
        cv.text((VX, 560), "DANCE GAME!", 150 * s, stroke=14)
    if t > 1.6:
        s2 = ease_out_back((t - 1.6) / 0.4)
        cv.text((VX, 720), "ARC LAB DANCE PARTY", 60 * s2, fill=(90, 220, 255), stroke=8)
    if t > 3.4:
        s3 = ease_out_back((t - 3.4) / 0.3)
        cv.text((VX, 830), "COPY THE MOVES • STAND UP!", 44 * s3, stroke=6)


def countdown(cv, t):
    for i, label in enumerate(("3", "2", "1", "GO!")):
        t0 = INTRO_END + i
        dt = t - t0
        if 0 <= dt < 1.0:
            s = ease_out_back(dt / 0.25)
            a = int(255 * min(1.0, (1 - dt) * 3))
            cv.text((VX, 470), label, (260 if i < 3 else 200) * s, fill=(255, 255, 255, a), stroke=16,
                    stroke_fill=(255, 60, 140, a))


def outro(cv, t):
    dt = t - OUTRO
    a = int(200 * clamp(dt / 0.5))
    cv.rect((0, 0, W, H), (10, 8, 30, a))
    s = ease_out_back(dt / 0.4)
    cv.text((VX, 260), "WORKOUT COMPLETE!", 110 * s, fill=(255, 230, 90), stroke=12)
    if dt > 0.8:
        s2 = ease_out_back((dt - 0.8) / 0.3)
        cv.rect((VX - 380 * s2, 360, VX + 380 * s2, 640), (255, 255, 255, 235), radius=40)
        cv.text((VX, 430), f"MAX COMBO  {len(CHART)}", 64 * s2, fill=INK)
        cv.text((VX, 520), "PERFECT  100%", 56 * s2, fill=(255, 60, 140))
        cv.text((VX, 595), "★ ★ ★", 56 * s2, fill=(255, 190, 0))
    if dt > 4.2:
        s3 = ease_out_back((dt - 4.2) / 0.3) * (1 + 0.04 * math.sin(dt * 8))
        cv.rect((VX - 330 * s3, 700, VX + 330 * s3, 800), (232, 40, 60), radius=50, outline=(255, 255, 255), width=5)
        cv.text((VX, 750), "FOLLOW ARC LAB", 60 * s3)
    rnd = random.Random(5)  # confetti
    for i in range(80):
        x = rnd.uniform(0, W) + math.sin(dt * 2 + i) * 40
        y = (rnd.uniform(-H, 0) + dt * rnd.uniform(160, 320)) % (H + 40) - 20
        c = [YELLOW, BLUE, PINK, GREEN][i % 4]
        cv.rect((x, y, x + 16, y + 10), c + (230,), radius=3)


# --------------------------------------------------------------- frame

class Renderer:
    def __init__(self):
        self.bg = make_background()

    def frame(self, t):
        if t < INTRO_END:
            img = Image.new("RGB", (W * SS, H * SS))
            cv = Cv(img)
            intro(cv, t)
            hud_only = True
        else:
            img = self.bg.copy()
            cv = Cv(img)
            stage_lights(cv, t)
            draw_arch(cv, t)
            draw_highway(cv, t)
            # tiles, far to near
            lo = int(np.searchsorted(CHART_T, t - 0.25))
            hi = int(np.searchsorted(CHART_T, t + LOOK * 1.2))
            for i in range(hi - 1, lo - 1, -1):
                T, kind = CHART[i]
                z = (T - t) / LOOK
                if z < -0.02:
                    continue  # consumed at the hit line
                draw_tile(cv, kind, z, LANES[kind])
            hit_fx(cv, t)
            move, ph = current_move(t)
            for idx, (name, C, origin, k, mirror) in sorted(enumerate(CREW), key=lambda e: e[1][2][1]):
                if t < GAME_START:
                    mv, pp = (None, 0.0)
                else:
                    mv, pp = move, ph
                pose = dance_pose(t, mv, pp, offset=idx * 0.05)
                # shadow
                sx = Cv(img)
                sx.ellipse((origin[0] - 120 * k, origin[1] - 18 * k, origin[0] + 120 * k, origin[1] + 18 * k),
                           fill=(120, 70, 40, 90))
                rig.draw_character(XCanvas(img, origin, k, mirror), pose, C, t + idx)
            judgement(cv, t)
            combo(cv, t)
            countdown(cv, t)
            banner(cv, t)
            if t >= OUTRO:
                outro(cv, t)
            hud_only = False
        hud(cv, t)
        out = img.convert("RGB").reduce(SS)
        if t < 0.25:
            out = Image.blend(Image.new("RGB", out.size), out, t / 0.25)
        if t > TOTAL - 0.6:
            out = Image.blend(out, Image.new("RGB", out.size), (t - (TOTAL - 0.6)) / 0.6)
        return out


def render_range(args):
    start, end, path = args
    r = Renderer()
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium",
           "-crf", "20", "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(start, end):
        proc.stdin.write(r.frame(i / FPS).tobytes())
        if (i - start) % (FPS * 10) == 0:
            print(f"[dance] worker {start:>5}: {(i - start) / max(1, end - start) * 100:5.1f}%",
                  file=sys.stderr, flush=True)
    proc.stdin.close()
    proc.wait()
    return path


# --------------------------------------------------------------- audio

SR = 44100


def env(n, a, r):
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(a, 1e-4)) * np.exp(-t / max(r, 1e-4))


def osc(freq, dur, kind="sine", detune=0.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    if kind == "saw":
        w = 2 * ((t * freq) % 1) - 1
        if detune:
            w = 0.5 * w + 0.5 * (2 * ((t * freq * (1 + detune)) % 1) - 1)
    elif kind == "square":
        w = np.where((t * freq) % 1 < 0.5, 1.0, -1.0)
    elif kind == "tri":
        w = 2 * np.abs(2 * ((t * freq) % 1) - 1) - 1
    else:
        w = np.sin(2 * np.pi * freq * t)
    return w


def mtof(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def lowpass(x, fc):
    a = math.exp(-2 * math.pi * fc / SR)
    # one-pole IIR via cumulative trick (vectorized with scipy-free approach: chunked)
    y = np.empty_like(x)
    acc = 0.0
    b = 1 - a
    for i in range(0, len(x), 4096):
        seg = x[i:i + 4096]
        # exact recursion inside the chunk using powers of a
        n = len(seg)
        powers = a ** np.arange(1, n + 1)
        conv = np.convolve(seg * b, a ** np.arange(n))[:n]
        y[i:i + n] = conv + acc * powers
        acc = y[i + n - 1]
    return y


class Mix:
    def __init__(self, dur):
        self.n = int(dur * SR) + SR
        self.bus = {k: np.zeros(self.n) for k in ("drums", "bass", "pad", "lead", "fx", "voice")}

    def add(self, bus, sig, t):
        i = int(t * SR)
        if i >= self.n or i + len(sig) <= 0:
            return
        j = min(self.n, i + len(sig))
        self.bus[bus][i:j] += sig[:j - i]


def kick():
    n = int(0.32 * SR)
    t = np.arange(n) / SR
    f = 48 + 120 * np.exp(-t * 32)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 8) * 0.9


def clap():
    rng = np.random.default_rng(7)
    n = int(0.25 * SR)
    x = rng.uniform(-1, 1, n)
    x = x - np.concatenate([[0], x[:-1]]) * 0.6
    e = np.zeros(n)
    for k in (0, 0.011, 0.022):
        i = int(k * SR)
        e[i:] += np.exp(-np.arange(n - i) / SR / (0.012 if k < 0.02 else 0.09))
    return x * e * 0.28


def hat(open_=False):
    rng = np.random.default_rng(3 if open_ else 4)
    n = int((0.2 if open_ else 0.05) * SR)
    x = rng.uniform(-1, 1, n)
    x = np.diff(x, prepend=0)
    return x * env(n, 0.001, 0.06 if open_ else 0.012) * (0.12 if open_ else 0.08)


def snare_roll(t0, dur, mix):
    rng = np.random.default_rng(9)
    hits = int(dur / (BEAT / 4))
    for i in range(hits):
        n = int(0.12 * SR)
        x = rng.uniform(-1, 1, n) * env(n, 0.001, 0.05)
        mix.add("drums", x * (0.05 + 0.2 * i / hits), t0 + i * BEAT / 4)


def riser(t0, dur, mix, vol=0.25):
    n = int(dur * SR)
    rng = np.random.default_rng(11)
    x = rng.uniform(-1, 1, n)
    x = np.diff(x, prepend=0) * np.linspace(0.3, 1, n)
    f = np.linspace(300, 2400, n)
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.15
    mix.add("fx", (x * 0.6 + s) * np.linspace(0, 1, n) ** 2 * vol, t0)


CHORDS = [  # per bar: C, G, Am, F  (bass root, pad notes)
    (36, (60, 64, 67)), (43, (59, 62, 67)), (45, (60, 64, 69)), (41, (60, 65, 69)),
]
HOOK = [  # eighth notes over 4 bars, 0 = rest
    76, 76, 79, 81, 79, 76, 74, 72,   74, 76, 79, 76, 74, 72, 74, 0,
    76, 76, 79, 81, 84, 81, 79, 76,   77, 76, 74, 72, 74, 72, 72, 0,
]
VERSE = [  # pluck arp pattern (index into chord tones + octave), 16ths
    0, 1, 2, 1, 0, 1, 2, 3, 0, 1, 2, 1, 2, 3, 2, 1,
]


def lead_note(m, dur):
    f = mtof(m)
    n = int(dur * SR)
    t = np.arange(n) / SR
    vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * t) * np.minimum(1, t / 0.15)
    ph = np.cumsum(f * vib) / SR
    w = 0.55 * np.where(ph % 1 < 0.5, 1.0, -1.0) + 0.45 * (2 * (ph % 1) - 1)
    return w * env(n, 0.006, dur * 0.9) * 0.085


def pluck(m, dur=0.25):
    n = int(dur * SR)
    w = osc(mtof(m), dur, "saw", detune=0.006) * env(n, 0.002, 0.07)
    return w * 0.06


def pad_chord(notes, dur):
    n = int(dur * SR)
    out = np.zeros(n)
    for m in notes:
        out += osc(mtof(m), dur, "saw", detune=0.008)
    e = np.minimum(1, np.arange(n) / SR / 0.08) * np.minimum(1, (n - np.arange(n)) / SR / 0.15)
    return out * e * 0.022


def bass_note(m, dur):
    n = int(dur * SR)
    w = osc(mtof(m), dur, "saw") * 0.6 + osc(mtof(m), dur, "sine") * 0.6
    return w * env(n, 0.003, 0.12) * 0.18


def synth_song(path, voice_clips=None, voice_events=None):
    mix = Mix(TOTAL)
    # ---- intro (0-6): pad + riser, countdown hits
    for b in range(3):
        root, notes = CHORDS[b % 4]
        mix.add("pad", pad_chord(notes, BAR) * 1.2, b * BAR)
    riser(0, INTRO_END, mix, 0.18)
    for i in range(4):
        t0 = INTRO_END + i
        mix.add("drums", kick() * (1.0 if i < 3 else 1.3), t0)
        mix.add("fx", osc(mtof(72 if i < 3 else 84), 0.35, "square") * env(int(0.35 * SR), 0.002, 0.12) * 0.07, t0)
    snare_roll(INTRO_END + 2, 2.0, mix)

    # ---- gameplay sections
    for si, (name, _, start, bars) in enumerate(SECTIONS):
        for bar in range(bars):
            t0 = start + bar * BAR
            root, notes = CHORDS[bar % 4]
            chorus = bar >= 8
            for beat in range(4):
                tb = t0 + beat * BEAT
                mix.add("drums", kick(), tb)
                mix.add("drums", hat(True), tb + BEAT / 2)
                if beat in (1, 3):
                    mix.add("drums", clap(), tb)
                for s16 in range(4):
                    if s16 != 2:
                        mix.add("drums", hat(), tb + s16 * BEAT / 4)
                for e8 in range(2):
                    mix.add("bass", bass_note(root + (12 if e8 else 0), BEAT / 2 * 0.95), tb + e8 * BEAT / 2)
            mix.add("pad", pad_chord(notes, BAR), t0)
            if chorus:
                for e in range(8):
                    m = HOOK[((bar - 8) % 4) * 8 + e]
                    if m:
                        nd = lead_note(m, BEAT / 2 * 0.92)
                        mix.add("lead", nd, t0 + e * BEAT / 2)
                        mix.add("lead", nd * 0.35, t0 + e * BEAT / 2 + BEAT * 0.75)   # echo
            else:
                tones = list(notes) + [notes[0] + 12]
                for s in range(16):
                    mix.add("lead", pluck(tones[VERSE[s]] + 12), t0 + s * BEAT / 4)
        # break (2 bars) after each section except the last: filtered pad, riser, roll
        end = start + bars * BAR
        if si < len(SECTIONS) - 1:
            for b in range(2):
                root, notes = CHORDS[b]
                mix.add("pad", pad_chord(notes, BAR) * 1.4, end + b * BAR)
                mix.add("bass", bass_note(root, BAR * 0.9) * 0.6, end + b * BAR)
            riser(end, 2 * BAR, mix, 0.2)
            snare_roll(end + BAR, BAR, mix)

    # ---- outro: big final chord, sparkles
    mix.add("drums", kick() * 1.3, OUTRO)
    for m in (48, 60, 64, 67, 72, 76):
        n = int(8 * SR)
        mix.add("pad", osc(mtof(m), 8, "saw", detune=0.01) * env(n, 0.02, 3.0) * 0.03, OUTRO)
    for k, m in enumerate((72, 76, 79, 84, 88, 91)):
        mix.add("fx", lead_note(m, 0.3), OUTRO + 0.15 + k * 0.09)

    # ---- sidechain pump on pad + bass + lead
    n = mix.n
    t = np.arange(n) / SR
    phase = np.where(t >= GAME_START, ((t - GAME_START) % BEAT), 10.0)
    duck = 1 - 0.55 * np.exp(-phase / 0.09)
    duck[t >= OUTRO] = 1.0
    music = mix.bus["drums"] + (mix.bus["bass"] + mix.bus["pad"] + mix.bus["lead"] * 1.1) * duck + mix.bus["fx"]

    # ---- voice on top, music ducks under it
    if voice_clips and voice_events:
        for (tv, line) in voice_events:
            clip = voice_clips.get(line)
            if clip is not None:
                mix.add("voice", clip, tv)
        v = mix.bus["voice"]
        envv = np.abs(v)
        k = int(0.05 * SR)
        envv = np.convolve(envv, np.ones(k) / k, mode="same")
        dv = 1 - 0.45 * np.clip(envv * 8, 0, 1)
        music = music * dv + v * 1.0

    music = np.tanh(music * 1.3)
    music /= np.abs(music).max() + 1e-9
    music *= 0.89
    fo = int(0.6 * SR)
    music[int(TOTAL * SR) - fo:int(TOTAL * SR)] *= np.linspace(1, 0, fo)
    music = music[:int(TOTAL * SR)]
    pcm = (music * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def load_voice(voice_dir: Path | None, work: Path):
    """Return {line_number: np.array}. Missing lines fall back to espeak-ng if available."""
    clips = {}
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    for i, text in enumerate(LINES, 1):
        src = None
        if voice_dir:
            for ext in ("wav", "mp3", "m4a", "ogg"):
                cand = voice_dir / f"{i:02d}.{ext}"
                if cand.exists():
                    src = cand
                    break
        if src is None and espeak:
            src = work / f"tts_{i:02d}.wav"
            subprocess.run([espeak, "-v", "en-us+f3", "-s", "175", "-p", "60", "-w", str(src), text],
                           capture_output=True)
        if src is None or not Path(src).exists():
            continue
        out = work / f"v_{i:02d}.raw"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(SR),
                        "-f", "s16le", str(out)], check=True)
        clips[i] = np.frombuffer(out.read_bytes(), dtype=np.int16).astype(np.float64) / 32768 * 0.9
    return clips


# --------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Render the Arc Lab Dance Party interactive workout video.")
    ap.add_argument("-o", "--output", default="arc-lab-dance-party.mp4")
    ap.add_argument("--voice-dir", help="folder with 01.wav ... coach clips (LINES order)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--still", type=float, help="render one frame at this time to PNG")
    ap.add_argument("--song-only", action="store_true", help="write only the song (no voice) as WAV")
    ap.add_argument("--preview", type=float, help="render only the first N seconds")
    ap.add_argument("--remux", help="existing rendered video: rebuild only the audio (e.g. new voice clips)")
    args = ap.parse_args()
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.still is not None:
        Renderer().frame(args.still).save(out.with_suffix(".png"))
        print(out.with_suffix(".png"))
        return 0
    work = Path(tempfile.mkdtemp(prefix="arcdance-"))
    try:
        if args.song_only:
            synth_song(out.with_suffix(".wav"))
            print(out.with_suffix(".wav"))
            return 0
        if args.remux:
            voice = load_voice(Path(args.voice_dir) if args.voice_dir else None, work)
            print(f"[dance] voice clips: {len(voice)}/{len(LINES)}", file=sys.stderr)
            wav = work / "mix.wav"
            synth_song(wav, voice, voice_schedule())
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", args.remux, "-i", str(wav), "-map", "0:v",
                            "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                            "-movflags", "+faststart", str(out)], check=True)
            print(out)
            return 0
        print(f"[dance] {len(CHART)} tiles, {TOTAL:.0f}s", file=sys.stderr)
        voice = load_voice(Path(args.voice_dir) if args.voice_dir else None, work)
        print(f"[dance] voice clips: {len(voice)}/{len(LINES)}", file=sys.stderr)
        wav = work / "mix.wav"
        synth_song(wav, voice, voice_schedule())
        synth_song(out.with_name(out.stem + "_song.wav"))  # instrumental, for reuse
        dur = min(TOTAL, args.preview or TOTAL)
        n = int(round(dur * FPS))
        k = max(1, args.workers)
        bounds = [(n * i // k, n * (i + 1) // k, work / f"part{i}.mp4") for i in range(k)]
        with mp.get_context("fork").Pool(k) as pool:
            parts = pool.map(render_range, bounds)
        lst = work / "parts.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                        "-b:a", "192k", "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)], check=True)
        Renderer().frame(SECTIONS[2][2] + 5.02).save(out.with_name(out.stem + "_thumb.png"))
        print(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

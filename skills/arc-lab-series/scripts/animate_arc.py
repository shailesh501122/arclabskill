#!/usr/bin/env python3
"""Arc Lab "Training Arc": a fully code-drawn animated short (9:16).

Every frame is drawn with Pillow from shapes; the soundtrack and sound effects are
synthesized with numpy. No camera, stock footage or AI image model is involved.

Story of Part 1 (about 28 s):
  1. Hook        - Day 1, Kai can't do a single push-up (low stats)
  2. Fail        - push-up attempt, arms shake, face-plant
  3. Montage     - running at dawn, days tick 2 -> 30, stats climb
  4. Payoff      - fast push-ups, rep counter hits 50
  5. Power-up    - aura, particles, shake, flash, LEVEL UP
  6. Hero + CTA  - final pose, "Part 2" tease

Usage
  python3 animate_arc.py -o training-arc-part1.mp4 [--config arc.json] [--preview 5.0]
The optional JSON config overrides texts and colors (see DEFAULTS below).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
SS = 2                      # supersampling factor (draw at 2x, downscale)
FPS = 30
G = 1430                    # ground line (1x coordinates)
OL = 5                      # outline width (1x)
INK = (17, 16, 28)

DEFAULTS = {
    "series": "ARC LAB",
    "arc": "TRAINING ARC",
    "part": 1,
    "name": "KAI",
    "handle": "@arclab",
    "lines": {
        "hook": ["Day 1.", "I couldn't do ONE push-up."],
        "fail": "0 push-ups.",
        "montage": "So I trained. Every. Single. Day.",
        "payoff": "Day 30.",
        "power": "Then something changed...",
        "hero": "This is only Part 1.",
        "cta": "FOLLOW FOR PART 2",
    },
    "colors": {
        "hoodie": "#FF7A1A", "pants": "#2B2D42", "skin": "#F4C7A1", "hair": "#22204A",
        "band": "#E8283C", "shoe": "#F5F5F5", "aura": "#3DF5FF", "accent": "#FFD23F",
    },
}

SCENES = [  # name, start, end
    ("hook", 0.0, 3.5),
    ("fail", 3.5, 8.0),
    ("montage", 8.0, 14.0),
    ("payoff", 14.0, 18.5),
    ("power", 18.5, 23.5),
    ("hero", 23.5, 28.0),
]
TOTAL = SCENES[-1][2]
FLASH_T = 22.4


# ============================================================== helpers

def hexc(v: str) -> tuple[int, int, int]:
    v = v.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def shade(c, k):
    return tuple(max(0, min(255, int(x * k))) for x in c)


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease(t):  # smoothstep
    t = clamp(t)
    return t * t * (3 - 2 * t)


def ease_out_back(t, s=1.7):
    t = clamp(t) - 1
    return t * t * ((s + 1) * t + s) + 1


def lerp(a, b, t):
    return a + (b - a) * t


def lerp2(p, q, t):
    return (lerp(p[0], q[0], t), lerp(p[1], q[1], t))


def add(p, q):
    return (p[0] + q[0], p[1] + q[1])


def sub(p, q):
    return (p[0] - q[0], p[1] - q[1])


def mul(p, k):
    return (p[0] * k, p[1] * k)


def norm(p):
    d = math.hypot(*p) or 1.0
    return (p[0] / d, p[1] / d)


def ik(root, target, l1, l2, pref):
    """Two-bone IK. Returns (joint, end). pref = world direction the joint should bulge to."""
    d = sub(target, root)
    dist = math.hypot(*d)
    dist = clamp(dist, abs(l1 - l2) + 1e-3, l1 + l2 - 1e-3)
    u = norm(d)
    end = add(root, mul(u, dist))
    a = (l1 * l1 - l2 * l2 + dist * dist) / (2 * dist)
    h = math.sqrt(max(l1 * l1 - a * a, 0))
    base = add(root, mul(u, a))
    n1 = (-u[1], u[0])
    j1 = add(base, mul(n1, h))
    j2 = sub(base, mul(n1, h))
    dot1 = (j1[0] - base[0]) * pref[0] + (j1[1] - base[1]) * pref[1]
    dot2 = (j2[0] - base[0]) * pref[0] + (j2[1] - base[1]) * pref[1]
    return (j1 if dot1 >= dot2 else j2), end


def find_font(bold=True):
    cands = (["/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
              "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf"] if bold else
             ["/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf",
              "C:/Windows/Fonts/arial.ttf"])
    for c in cands:
        if Path(c).is_file():
            return c
    return None


_FONTS: dict = {}


def font(size, bold=True):
    key = (size, bold)
    if key not in _FONTS:
        path = find_font(bold)
        _FONTS[key] = ImageFont.truetype(path, size * SS) if path else ImageFont.load_default(size * SS)
    return _FONTS[key]


# ============================================================== canvas (1x coords -> 2x pixels)

class Canvas:
    def __init__(self, img: Image.Image):
        self.img = img
        self.d = ImageDraw.Draw(img, "RGBA")

    @staticmethod
    def P(p):
        return (p[0] * SS, p[1] * SS)

    def poly(self, pts, fill, outline=INK, width=OL):
        pts = [self.P(p) for p in pts]
        if outline is not None and width:
            self.d.polygon(pts, fill=fill, outline=outline, width=int(width * SS))
        else:
            self.d.polygon(pts, fill=fill)

    def circle(self, c, r, fill, outline=INK, width=OL):
        x, y = self.P(c)
        r *= SS
        if outline is not None and width:
            w = width * SS
            self.d.ellipse((x - r - w, y - r - w, x + r + w, y + r + w), fill=outline)
        self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def seg(self, a, b, w, fill, outline=INK, ow=OL):
        """Thick capsule segment with optional outline."""
        if outline is not None and ow:
            self._capsule(a, b, w / 2 + ow, outline)
        self._capsule(a, b, w / 2, fill)

    def _capsule(self, a, b, r, fill):
        A, B = self.P(a), self.P(b)
        rr = r * SS
        self.d.line([A, B], fill=fill, width=int(2 * rr))
        for (x, y) in (A, B):
            self.d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill)

    def polyline(self, pts, w, fill):
        pts = [self.P(p) for p in pts]
        self.d.line(pts, fill=fill, width=int(w * SS), joint="curve")
        r = w * SS / 2
        for (x, y) in (pts[0], pts[-1]):
            self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def rect(self, box, fill, radius=0, outline=None, width=0):
        b = tuple(v * SS for v in box)
        self.d.rounded_rectangle(b, radius=radius * SS, fill=fill,
                                 outline=outline, width=int(width * SS))

    def text(self, xy, s, size, fill=(255, 255, 255), anchor="mm", stroke=8,
             stroke_fill=INK, bold=True):
        self.d.text(self.P(xy), s, font=font(size, bold), fill=fill, anchor=anchor,
                    stroke_width=int(stroke * SS), stroke_fill=stroke_fill)

    def textlen(self, s, size):
        return self.d.textlength(s, font=font(size)) / SS


# ============================================================== character

L_TORSO, L_UARM, L_FARM, L_THIGH, L_SHIN = 165, 86, 80, 104, 100
HEAD_R = 80


class Pose:
    def __init__(self, **kw):
        self.hip = kw.get("hip", (540, G - 214))
        self.lean = kw.get("lean", 0.0)          # degrees, + leans forward (right)
        self.shoulder = kw.get("shoulder")        # optional explicit shoulder point
        self.tilt = kw.get("tilt", 0.0)          # head tilt relative to torso
        self.hands = kw.get("hands")              # [near, far] targets
        self.feet = kw.get("feet")                # [near, far] ankle targets
        self.eye = kw.get("eye", 1.0)            # 0 closed .. 1 open
        self.brow = kw.get("brow", 0.0)          # -1 sad .. +1 fierce
        self.mouth = kw.get("mouth", "flat")     # flat | open | grit | smile | o
        self.sweat = kw.get("sweat", 0.0)
        self.blush = kw.get("blush", 0.0)
        self.wind = kw.get("wind", 0.4)
        self.fist = kw.get("fist", True)
        self.glow = kw.get("glow", 0.0)


def shoulder_of(p: Pose):
    if p.shoulder:
        return p.shoulder
    a = math.radians(p.lean)
    return (p.hip[0] + math.sin(a) * L_TORSO, p.hip[1] - math.cos(a) * L_TORSO)


def default_limbs(p: Pose):
    sh = shoulder_of(p)
    if p.hands is None:
        p.hands = [(sh[0] + 30, sh[1] + 150), (sh[0] - 18, sh[1] + 150)]
    if p.feet is None:
        p.feet = [(p.hip[0] + 26, G - 14), (p.hip[0] - 22, G - 14)]
    return sh


def draw_character(cv: Canvas, p: Pose, C: dict, t: float):
    sh = default_limbs(p)
    hip = p.hip
    u = norm(sub(sh, hip))                    # torso up
    front = (-u[1], u[0])                     # chest direction
    torso_deg = math.degrees(math.atan2(u[0], -u[1]))

    hoodie, pants, skin = hexc(C["hoodie"]), hexc(C["pants"]), hexc(C["skin"])
    shoe = hexc(C["shoe"])

    def leg(foot_t, dark):
        k = 0.72 if dark else 1.0
        h = add(hip, mul(front, -6 if dark else 6))
        knee, ank = ik(h, foot_t, L_THIGH, L_SHIN, pref=(front[0] * 0.6 + u[0] * 0.4 + 0.5, front[1] * 0.6 + u[1] * 0.4))
        cv.seg(h, knee, 40, shade(pants, k))
        cv.seg(knee, ank, 34, shade(pants, k))
        # shoe: points along the shin's forward perpendicular, flattened toward the ground
        sd = norm(sub(ank, knee))
        toe_dir = norm((-sd[1] if front[0] >= 0 else sd[1], sd[0] if front[0] >= 0 else -sd[0]))
        if toe_dir[0] < 0:
            toe_dir = (-toe_dir[0], -toe_dir[1])
        toe = add(ank, mul(toe_dir, 34))
        cv.seg(add(ank, mul(toe_dir, -6)), toe, 26, shade(shoe, k))
        cv.seg(add(add(ank, mul(toe_dir, -8)), mul(sd, 10)), add(toe, mul(sd, 10)), 8,
               shade((70, 70, 90), k), outline=None)

    def arm(hand_t, dark):
        k = 0.72 if dark else 1.0
        s = add(sh, mul(front, -8 if dark else 4))
        elbow, hand = ik(s, hand_t, L_UARM, L_FARM, pref=(-front[0] + 0.15 * u[0] * -1, -front[1] + 0.35))
        cv.seg(s, elbow, 34, shade(hoodie, k))
        cv.seg(elbow, hand, 26, shade(skin, k))
        # cuff
        cd = norm(sub(hand, elbow))
        cv.seg(add(elbow, mul(cd, -4)), add(elbow, mul(cd, 12)), 34, shade(hoodie, k * 0.9), ow=3)
        cv.circle(hand, 19 if p.fist else 16, shade(skin, k))
        return hand

    # back limbs
    leg(p.feet[1], True)
    arm(p.hands[1], True)

    # torso (hoodie)
    hw, sw = 28, 36
    pts = [add(hip, mul(front, -hw)), add(hip, mul(front, hw)),
           add(add(sh, mul(front, sw)), mul(u, -10)), add(sh, mul(front, sw * 0.7)),
           add(sh, mul(front, -sw * 0.8)), add(add(sh, mul(front, -sw)), mul(u, -14))]
    cv.poly(pts, hoodie)
    # hood lump behind the neck
    cv.circle(add(add(sh, mul(front, -22)), mul(u, 2)), 22, shade(hoodie, 0.85))
    # pocket + chest chevron (series emblem)
    pc = add(hip, mul(u, 48))
    cv.poly([add(pc, mul(front, 4)), add(add(pc, mul(front, 24)), mul(u, 18)),
             add(add(pc, mul(front, 24)), mul(u, -6))], shade(hoodie, 0.82), width=3)
    ch = add(add(hip, mul(u, 112)), mul(front, 12))
    cv.polyline([add(add(ch, mul(front, -9)), mul(u, 8)), ch, add(add(ch, mul(front, -9)), mul(u, -8))],
                7, (255, 255, 255))
    # belt
    cv.seg(add(hip, mul(front, -hw)), add(hip, mul(front, hw)), 12, shade(pants, 0.7), ow=3)

    # front limbs
    leg(p.feet[0], False)
    hand_near = arm(p.hands[0], False)

    # neck + head
    neck_top = add(sh, mul(u, 16))
    cv.seg(sh, neck_top, 26, skin, ow=4)
    head_ang = torso_deg + p.tilt
    hc = add(neck_top, rot((0, -HEAD_R + 6), head_ang))
    draw_head(cv, hc, head_ang, p, C, t)
    # near arm in front of head only if raised above it
    if hand_near[1] < hc[1] + HEAD_R * 0.3:
        arm(p.hands[0], False)


def rot(v, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)


def draw_head(cv: Canvas, hc, ang, p: Pose, C: dict, t: float):
    R = HEAD_R
    skin, hair, band = hexc(C["skin"]), hexc(C["hair"]), hexc(C["band"])

    def L(x, y):  # local head coords (x = facing direction, y down) -> world
        v = rot((x, y), ang)
        return (hc[0] + v[0], hc[1] + v[1])

    def ell(cx, cy, rx, ry, n=20):
        return [L(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n))
                for i in range(n)]

    # headband tails (behind head), flutter in wind
    knot = (-R * 0.93, -R * 0.36)
    for k, off in ((0, 0.0), (1, 1.3)):
        pts = []
        for i in range(8):
            fl = math.sin(t * (9 + 6 * p.wind) + i * 0.9 + off) * (6 + 16 * p.wind) * (i / 7)
            pts.append(L(knot[0] - i * (16 + 6 * p.wind), knot[1] + i * (9 - 6 * p.wind) + fl + k * 10))
        cv.polyline(pts, 20, INK)
        cv.polyline(pts, 20 - 2 * OL * 0.8, band if k == 0 else shade(band, 0.8))

    # ear + face
    cv.circle(hc, R, skin)
    # hair: spiky crown sweeping backward + bangs
    outer, inner = [], []
    n = 15
    for i in range(n + 1):
        phi = math.radians(-25 - 205 * i / n)          # front-top -> top -> back-low
        spike = 1.38 if i % 2 else 1.05
        rx, ry = R * spike * math.cos(phi), R * spike * math.sin(phi)
        if i % 2:
            rx -= R * 0.22                                # tips swept back
        outer.append(L(rx, ry))
    for i in range(n + 1):
        phi = math.radians(-230 + 205 * i / n)
        frac = i / n
        if frac < 0.45:   # back of the head: hair follows the skull
            r_in = 0.98
        else:             # forehead: zig-zag bangs
            r_in = 0.60 if i % 2 else 0.78
        inner.append(L(R * r_in * math.cos(phi), R * r_in * math.sin(phi)))
    cv.poly(outer + inner, hair)
    # ear
    cv.poly(ell(-R * 0.42, R * 0.18, R * 0.13, R * 0.18), shade(skin, 0.92), width=4)
    cv.polyline([L(-R * 0.40, R * 0.10), L(-R * 0.46, R * 0.2), L(-R * 0.40, R * 0.26)], 4, shade(skin, 0.7))

    # headband across forehead
    cv.seg(L(-R * 0.95, -R * 0.36), L(R * 0.93, -R * 0.42), 22, band)
    cv.circle(L(*knot), 12, shade(band, 0.85), width=4)
    # band plate
    cv.poly([L(R * 0.42, -R * 0.52), L(R * 0.78, -R * 0.54), L(R * 0.78, -R * 0.30), L(R * 0.42, -R * 0.30)],
            (210, 214, 230), width=3)

    # eye
    open_ = max(0.08, p.eye)
    cv.poly(ell(R * 0.47, R * 0.02, R * 0.17, R * 0.24 * open_), (255, 255, 255), width=4)
    if open_ > 0.2:
        cv.poly(ell(R * 0.53, R * 0.04, R * 0.09, R * 0.15 * open_), INK, outline=None, width=0)
        cv.circle(L(R * 0.50, -R * 0.03 * open_), R * 0.035, (255, 255, 255), outline=None)
        if p.glow > 0:
            cv.poly(ell(R * 0.53, R * 0.04, R * 0.05, R * 0.09 * open_), hexc(C["aura"]), outline=None, width=0)
    # brow
    b = p.brow
    cv.polyline([L(R * 0.28, -R * 0.24 - b * R * 0.06), L(R * 0.66, -R * 0.24 + b * R * 0.10)], 9, INK)
    # nose
    cv.polyline([L(R * 0.92, R * 0.12), L(R * 1.0, R * 0.22), L(R * 0.9, R * 0.26)], 5, shade(skin, 0.7))
    # mouth
    m = p.mouth
    if m == "open":
        cv.poly(ell(R * 0.70, R * 0.48, R * 0.11, R * 0.10), (120, 30, 40), width=4)
    elif m == "o":
        cv.poly(ell(R * 0.72, R * 0.48, R * 0.07, R * 0.08), (120, 30, 40), width=4)
    elif m == "grit":
        cv.poly([L(R * 0.52, R * 0.40), L(R * 0.88, R * 0.40), L(R * 0.86, R * 0.56), L(R * 0.54, R * 0.56)],
                (255, 255, 255), width=4)
        cv.polyline([L(R * 0.53, R * 0.48), L(R * 0.87, R * 0.48)], 3, INK)
    elif m == "smile":
        cv.polyline([L(R * 0.52, R * 0.40), L(R * 0.66, R * 0.52), L(R * 0.86, R * 0.42)], 7, INK)
    else:
        cv.polyline([L(R * 0.58, R * 0.48), L(R * 0.84, R * 0.46 + (0.05 if b < 0 else 0) * R)], 6, INK)
    if p.blush > 0:
        a = int(110 * p.blush)
        cv.poly(ell(R * 0.32, R * 0.32, R * 0.14, R * 0.07), (255, 90, 110, a), outline=None, width=0)
    if p.sweat > 0:
        for i, (sx, sy) in enumerate(((-0.55, -0.85), (0.15, -1.15), (-0.95, -0.2))):
            ph = (t * 1.6 + i * 0.37) % 1.0
            if ph > p.sweat:
                continue
            c = L(R * sx * (1 + ph * 0.5), R * sy + ph * 40)
            cv.poly([add(c, (0, -16)), add(c, (9, 2)), add(c, (0, 10)), add(c, (-9, 2))],
                    (150, 220, 255), width=3)


# ============================================================== backgrounds

def gradient(top, bottom, h=H):
    col = np.linspace(0, 1, h * SS)[:, None]
    arr = (np.array(top)[None, :] * (1 - col) + np.array(bottom)[None, :] * col).astype(np.uint8)
    arr = np.repeat(arr[:, None, :], W * SS, axis=1)
    return Image.fromarray(arr, "RGB")


def make_skyline(seed, n, hmin, hmax, wmin, wmax):
    rnd = random.Random(seed)
    out, x = [], -200
    while x < W * 3:
        w = rnd.randint(wmin, wmax)
        out.append((x, w, rnd.randint(hmin, hmax), rnd.random()))
        x += w + rnd.randint(4, 30)
    return out


SKY_FAR = make_skyline(1, 0, 180, 420, 70, 150)
SKY_NEAR = make_skyline(2, 0, 120, 300, 90, 190)


def draw_skyline(cv, blds, base_y, color, offset=0.0, windows=None, period=W * 3):
    for (x, w, h, r) in blds:
        xx = ((x - offset) % period) - 200
        if xx > W + 50 or xx + w < -50:
            continue
        cv.rect((xx, base_y - h, xx + w, base_y + 2), color)
        if windows:
            rr = random.Random(int(x))
            for wy in range(int(base_y - h + 20), int(base_y - 20), 34):
                for wx in range(int(xx + 12), int(xx + w - 16), 26):
                    if rr.random() < windows[1]:
                        cv.rect((wx, wy, wx + 12, wy + 16), windows[0])


def bg_night():
    img = gradient((10, 12, 38), (38, 24, 70))
    cv = Canvas(img)
    rnd = random.Random(7)
    for _ in range(90):
        x, y = rnd.uniform(0, W), rnd.uniform(0, 900)
        cv.circle((x, y), rnd.uniform(1, 3), (255, 255, 255, rnd.randint(80, 220)), outline=None)
    cv.circle((820, 330), 90, (250, 244, 220), outline=None)
    cv.circle((790, 310), 90 * 0.85, (232, 225, 205, 90), outline=None)
    draw_skyline(cv, SKY_FAR, G - 120, (30, 26, 64))
    draw_skyline(cv, SKY_NEAR, G - 60, (22, 18, 48), windows=((255, 214, 120, 200), 0.18))
    rooftop(cv, (54, 52, 86))
    return img


def bg_dawn():
    return gradient((255, 140, 90), (255, 214, 150))


def bg_day():
    img = gradient((88, 170, 255), (190, 230, 255))
    cv = Canvas(img)
    for cx, cy, s in ((200, 420, 1.0), (760, 300, 0.8), (930, 640, 0.6)):
        for dx, dy, r in ((0, 0, 70), (60, 15, 55), (-60, 20, 50), (20, -35, 50)):
            cv.circle((cx + dx * s, cy + dy * s), r * s, (255, 255, 255, 235), outline=None)
    draw_skyline(cv, SKY_FAR, G - 120, (140, 170, 215))
    draw_skyline(cv, SKY_NEAR, G - 60, (110, 135, 185))
    rooftop(cv, (120, 118, 150))
    return img


def bg_sunset():
    img = gradient((70, 30, 110), (255, 150, 70))
    cv = Canvas(img)
    cv.circle((540, 1180), 260, (255, 210, 110), outline=None)
    for i in range(16):  # sun rays
        a = math.radians(i * 22.5)
        p1 = (540 + math.cos(a) * 300, 1180 + math.sin(a) * 300)
        p2 = (540 + math.cos(a + 0.08) * 1400, 1180 + math.sin(a + 0.08) * 1400)
        p3 = (540 + math.cos(a - 0.08) * 1400, 1180 + math.sin(a - 0.08) * 1400)
        cv.poly([p1, p2, p3], (255, 230, 160, 40), outline=None, width=0)
    draw_skyline(cv, SKY_FAR, G - 120, (120, 50, 100))
    draw_skyline(cv, SKY_NEAR, G - 60, (80, 30, 80), windows=((255, 200, 120, 150), 0.1))
    rooftop(cv, (60, 30, 70))
    return img


def rooftop(cv, color):
    cv.rect((-10, G - 6, W + 10, H + 10), color, outline=None)
    cv.rect((-10, G - 6, W + 10, G + 12), shade(color, 1.35))
    for x in range(-40, W, 180):
        cv.rect((x, G + 12, x + 4, H), shade(color, 0.8))


# ============================================================== HUD (text, stats, counters)

def caption(cv, lines, y, size=74, appear=1.0, color=(255, 255, 255), hl=None):
    if isinstance(lines, str):
        lines = [lines]
    s = ease_out_back(appear)
    sz = max(8, int(size * (0.6 + 0.4 * s)))
    for i, ln in enumerate(lines):
        fill = color
        if hl and hl in ln:
            fill = hl_color = color
        cv.text((W / 2, y + i * size * 1.18), ln, sz, fill=fill, stroke=10)


def caption_words(cv, words, y, t_local, per_word=0.22, size=80, hl_word=None, accent=(255, 210, 63)):
    """Words pop in one at a time, centered on one line (wraps to two)."""
    shown = [w for i, w in enumerate(words) if t_local >= i * per_word]
    if not shown:
        return
    lines, cur = [], []
    for w in shown:
        trial = " ".join(cur + [w])
        if cv.textlen(trial, size) > W - 140 and cur:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    lines.append(cur)
    for li, ln in enumerate(lines):
        total = cv.textlen(" ".join(ln), size)
        x = W / 2 - total / 2
        for w in ln:
            idx = words.index(w)
            pop = ease_out_back((t_local - idx * per_word) / 0.18)
            sz = max(8, int(size * (0.5 + 0.5 * pop)))
            fill = accent if (hl_word and hl_word in w) else (255, 255, 255)
            wl = cv.textlen(w, size)
            cv.text((x + wl / 2, y + li * size * 1.2), w, sz, fill=fill, stroke=10)
            x += wl + cv.textlen(" ", size)


def part_pill(cv, cfg, y=190):
    txt = f"{cfg['arc']}  ·  PART {cfg['part']}"
    tw = cv.textlen(txt, 40)
    cv.rect((W / 2 - tw / 2 - 30, y - 34, W / 2 + tw / 2 + 30, y + 34), hexc(cfg["colors"]["accent"]),
            radius=34, outline=INK, width=5)
    cv.text((W / 2, y), txt, 40, fill=INK, stroke=0)
    cv.text((W / 2, y - 80), cfg["series"], 34, fill=(255, 255, 255), stroke=6)


def stats_panel(cv, cfg, vals, lvl, y=1560):
    cv.rect((70, y, W - 70, y + 250), (12, 12, 30, 200), radius=28, outline=(255, 255, 255, 60), width=3)
    cv.text((110, y + 42), f"{cfg['name']}", 40, anchor="lm", stroke=0)
    cv.text((W - 110, y + 42), f"LVL {lvl}", 40, fill=hexc(cfg["colors"]["accent"]), anchor="rm", stroke=0)
    colors = [(255, 92, 92), (90, 200, 255), (160, 255, 120)]
    for i, (name, v) in enumerate(zip(("STR", "SPD", "WILL"), vals)):
        yy = y + 100 + i * 50
        cv.text((110, yy), name, 30, fill=(200, 205, 230), anchor="lm", stroke=0)
        x0, x1 = 220, W - 200
        cv.rect((x0, yy - 13, x1, yy + 13), (255, 255, 255, 40), radius=13)
        cv.rect((x0, yy - 13, x0 + (x1 - x0) * clamp(v / 99), yy + 13), colors[i], radius=13)
        cv.text((W - 110, yy), "MAX" if v >= 99 else str(int(v)), 32, anchor="rm", stroke=0)


def counter(cv, label, value, x, y, color, size=120):
    cv.text((x, y - size * 0.75), label, 40, fill=(255, 255, 255), stroke=7)
    cv.text((x, y), str(value), size, fill=color, stroke=12)


def speed_lines(cv, t, n=26, color=(255, 255, 255, 110)):
    rnd = random.Random(3)
    for i in range(n):
        y = rnd.uniform(380, G - 40)
        ln = rnd.uniform(120, 360)
        sp = rnd.uniform(1800, 3200)
        x = W + 200 - ((t * sp + rnd.uniform(0, 2000)) % (W + 800))
        cv.seg((x, y), (x + ln, y), rnd.uniform(3, 7), color, outline=None)


def dust(cv, t0, t, origin, n=14, color=(220, 220, 235)):
    dt = t - t0
    if dt < 0 or dt > 0.8:
        return
    rnd = random.Random(int(t0 * 100))
    for _ in range(n):
        a = rnd.uniform(math.pi * 1.05, math.pi * 1.95)
        sp = rnd.uniform(120, 420)
        p = (origin[0] + math.cos(a) * sp * dt * 1.4, origin[1] + math.sin(a) * sp * dt * 0.8)
        r = rnd.uniform(10, 26) * (1 - dt / 0.8)
        cv.circle(p, r, color + (int(200 * (1 - dt / 0.8)),), outline=None)


# ============================================================== scenes

def stand_pose(**kw):
    return Pose(**kw)


def scene_hook(cv, t, lt, cfg, C):
    breathe = math.sin(t * 2.2) * 3
    p = Pose(hip=(540, G - 210 + breathe * 0.3), lean=14, tilt=22, eye=0.55, brow=-1,
             mouth="flat", wind=0.15)
    sh = shoulder_of(p)
    p.hands = [(sh[0] + 14, sh[1] + 162), (sh[0] - 6, sh[1] + 162)]
    p.eye = 0.05 if (lt % 2.4) > 2.25 else 0.55
    draw_character(cv, p, C, t)
    part_pill(cv, cfg)
    caption_words(cv, cfg["lines"]["hook"][0].split(), 470, lt - 0.2, size=110)
    caption_words(cv, cfg["lines"]["hook"][1].split(), 610, lt - 0.8, size=72, hl_word="ONE",
                  accent=hexc(C["accent"]))
    stats_panel(cv, cfg, (3, 2, 9), 1)


def pushup_pose(depth, shake=0.0, t=0.0, face_plant=0.0):
    """depth 0 = arms extended, 1 = chest near floor."""
    feet = (300, G - 14)
    hands = (660, G - 18)
    sh_y = lerp(G - 178, G - 92, depth)
    sh_y = lerp(sh_y, G - 40, face_plant)
    jitter = math.sin(t * 70) * shake * 6
    sh = (hands[0] - 8 + jitter, sh_y + jitter * 0.5)
    # hip on the straight line feet->shoulder at leg length
    d = sub(sh, feet)
    hip = add(feet, mul(norm(d), (L_THIGH + L_SHIN) * 0.97))
    p = Pose(hip=hip, shoulder=sh, tilt=-62 + 20 * face_plant,
             feet=[feet, (feet[0] - 10, feet[1])],
             hands=[hands, (hands[0] - 20, hands[1])], fist=False, wind=0.1)
    if face_plant > 0.5:
        p.hands = [(sh[0] + 60, G - 18), (sh[0] - 40, G - 18)]
    return p


def scene_fail(cv, t, lt, cfg, C):
    # 0-0.6 get into position, 0.6-2.4 struggle down, 2.4-2.6 collapse, then lie
    if lt < 2.3:
        depth = ease(clamp((lt - 0.4) / 1.9)) * 0.55
        shake = clamp((lt - 0.8) / 1.2)
        p = pushup_pose(depth, shake=shake, t=t)
        p.mouth, p.brow, p.blush, p.sweat = "grit", 0.6, shake, shake
        p.eye = 0.6
    else:
        fall = ease(clamp((lt - 2.3) / 0.18))
        p = pushup_pose(0.55, face_plant=fall, t=t)
        p.mouth, p.eye, p.brow = ("o", 0.05, -1) if lt > 2.5 else ("open", 1.0, -1)
    draw_character(cv, p, C, t)
    dust(cv, SCENES[1][1] + 2.45, t, (700, G - 10))
    if lt > 2.45:
        pop = ease_out_back((lt - 2.45) / 0.2)
        cv.text((760, G - 260), "THUD!", int(110 * max(pop, 0.05)), fill=hexc(C["accent"]), stroke=12)
    if lt > 3.0:
        caption_words(cv, cfg["lines"]["fail"].split(), 520, lt - 3.0, size=110, hl_word="0",
                      accent=(255, 92, 92))
    else:
        caption_words(cv, ["Day", "1."], 520, lt, size=110)
    stats_panel(cv, cfg, (3, 2, 9), 1)


def run_pose(t, speed=2.3, hipx=520):
    ph = t * speed * 2 * math.pi
    bob = abs(math.cos(ph)) * 16
    hip = (hipx, G - 218 + bob)
    p = Pose(hip=hip, lean=16, tilt=-10, mouth="grit", brow=0.7, eye=0.9, wind=0.9)
    sh = shoulder_of(p)
    feet = []
    for k in (0, math.pi):
        q = ph + k
        fx = hip[0] + 10 + 92 * math.cos(q)
        fy = G - 14 - 70 * max(0, -math.sin(q))
        feet.append((fx, fy))
    hands = []
    for k in (math.pi, 0):
        q = ph + k
        hands.append((sh[0] + 30 + 70 * math.cos(q), sh[1] + 104 - 34 * math.sin(q) * math.cos(q) - 20))
    p.feet, p.hands = feet, hands
    return p


def scene_montage(cv, t, lt, cfg, C):
    # dawn sky to day, parallax city, speed lines, days ticking
    k = clamp(lt / 6)
    sky_off = lt * 120
    draw_skyline(cv, SKY_FAR, G - 120, mix((200, 100, 110), (140, 170, 215), k), offset=sky_off * 0.4)
    draw_skyline(cv, SKY_NEAR, G - 60, mix((150, 70, 100), (110, 135, 185), k), offset=sky_off)
    roof_c = mix((110, 70, 100), (120, 118, 150), k)
    cv.rect((-10, G - 6, W + 10, H + 10), roof_c)
    cv.rect((-10, G - 6, W + 10, G + 12), shade(roof_c, 1.35))
    for x in range(-200, W + 200, 180):
        xx = (x - lt * 900) % (W + 360) - 180
        cv.rect((xx, G + 12, xx + 5, H), shade(roof_c, 0.8))
    speed_lines(cv, t)
    p = run_pose(t)
    p.sweat = 0.6
    draw_character(cv, p, C, t)
    day = int(lerp(2, 30, ease(clamp((lt - 0.6) / 4.8))))
    counter(cv, "DAY", day, W / 2, 820, hexc(C["accent"]), size=170)
    caption_words(cv, cfg["lines"]["montage"].split(), 480, lt - 0.2, per_word=0.28, size=70,
                  hl_word="Every", accent=hexc(C["accent"]))
    s = ease(clamp(lt / 6))
    stats_panel(cv, cfg, (lerp(3, 38, s), lerp(2, 45, s), lerp(9, 60, s)), 1 + int(4 * s))


def scene_payoff(cv, t, lt, cfg, C):
    period = 0.42
    reps = int(lt / period)
    ph = (lt % period) / period
    depth = math.sin(ph * math.pi) ** 2 * 0.95
    p = pushup_pose(depth, t=t)
    p.mouth, p.brow, p.eye, p.sweat = "grit", 1.0, 0.9, 0.5
    draw_character(cv, p, C, t)
    rep = min(50, 40 + reps)
    pop = ease_out_back(clamp(ph * 3))
    counter(cv, "PUSH-UPS", rep, W / 2, 860, (160, 255, 120) if rep >= 50 else (255, 255, 255),
            size=int(170 * (0.85 + 0.15 * pop)))
    caption_words(cv, cfg["lines"]["payoff"].split(), 480, lt - 0.1, size=110)
    stats_panel(cv, cfg, (lerp(38, 55, clamp(lt / 4)), 45, 70), 5)


def aura(cv, center, t, level, color):
    if level <= 0:
        return
    rnd = random.Random(int(t * 15))
    layer = Image.new("RGBA", cv.img.size, (0, 0, 0, 0))
    lc = Canvas(layer)
    for ring, (sc, al) in enumerate(((1.25, 70), (1.0, 120), (0.75, 170))):
        pts = []
        n = 28
        for i in range(n):
            a = 2 * math.pi * i / n
            flick = 1 + 0.25 * math.sin(t * 22 + i * 1.7 + ring) + rnd.uniform(-0.08, 0.08)
            up = 1.0 + 0.9 * max(0, -math.sin(a))      # flames lick upward
            rx, ry = 190 * sc * flick * level, 330 * sc * flick * up * level
            pts.append((center[0] + math.cos(a) * rx, center[1] + math.sin(a) * ry * 0.85))
        lc.poly(pts, color + (int(al * level),), outline=None, width=0)
    layer = layer.filter(ImageFilter.GaussianBlur(14 * SS))
    cv.img.alpha_composite(layer) if cv.img.mode == "RGBA" else cv.img.paste(layer, (0, 0), layer)


def particles(cv, t, level, color, n=60):
    if level <= 0:
        return
    rnd = random.Random(11)
    for i in range(n):
        x0 = rnd.uniform(260, 820)
        sp = rnd.uniform(300, 900)
        ph = (t * sp / 1400 + rnd.random()) % 1.0
        y = G + 40 - ph * 1300
        x = x0 + math.sin(t * 3 + i) * 30
        r = rnd.uniform(4, 11) * level * (1 - ph * 0.6)
        cv.circle((x, y), r, color + (int(230 * (1 - ph)),), outline=None)


def scene_power(cv, t, lt, cfg, C):
    aura_c = hexc(C["aura"])
    charge = ease(clamp((lt - 0.4) / 3.3))
    burst = t >= FLASH_T
    level = charge if not burst else 1.15
    # burst lines in the background
    if charge > 0.1:
        rnd = random.Random(5)
        for i in range(36):
            a = 2 * math.pi * i / 36 + t * 0.6
            r0 = 260
            r1 = 1600
            w = 0.03 + rnd.uniform(0, 0.02)
            c = (540, 1100)
            cv.poly([add(c, (math.cos(a) * r0, math.sin(a) * r0)),
                     add(c, (math.cos(a - w) * r1, math.sin(a - w) * r1)),
                     add(c, (math.cos(a + w) * r1, math.sin(a + w) * r1))],
                    aura_c + (int(55 * charge),), outline=None, width=0)
    crouch = 26 * charge if not burst else 0
    p = Pose(hip=(540, G - 212 + crouch), lean=6 if not burst else -2, tilt=-4,
             eye=1.0, brow=1.0, mouth="grit" if not burst else "open", wind=1.0, glow=charge)
    sh = shoulder_of(p)
    if not burst:
        p.hands = [(sh[0] + 50, sh[1] + 120), (sh[0] - 30, sh[1] + 120)]
        p.feet = [(p.hip[0] + 60, G - 14), (p.hip[0] - 60, G - 14)]
    else:
        p.hands = [(sh[0] + 120, sh[1] - 40), (sh[0] - 110, sh[1] - 30)]
        p.feet = [(p.hip[0] + 70, G - 14), (p.hip[0] - 70, G - 14)]
    aura(cv, (540, G - 330), t, level, aura_c)
    particles(cv, t, level, aura_c)
    # ground cracks
    if charge > 0.5:
        rnd = random.Random(9)
        for i in range(6):
            x = 540 + (i - 2.5) * 90
            pts = [(x, G)]
            for j in range(4):
                pts.append((pts[-1][0] + rnd.uniform(-40, 40), pts[-1][1] + 30 + j * 10))
            cv.polyline(pts, 5, (20, 20, 30, int(255 * (charge - 0.5) * 2)))
    draw_character(cv, p, C, t)
    if not burst:
        caption_words(cv, cfg["lines"]["power"].split(), 480, lt - 0.1, per_word=0.3, size=70)
    else:
        pop = ease_out_back((t - FLASH_T) / 0.3)
        cv.text((W / 2, 640), "LEVEL UP!", int(150 * max(pop, 0.05)), fill=hexc(C["accent"]), stroke=14)
        cv.text((W / 2, 790), "LVL 5  →  LVL 10", int(60 * max(pop, 0.05)), stroke=8)
    s = ease(clamp((t - FLASH_T) / 0.6))
    stats_panel(cv, cfg, (lerp(55, 99, s), lerp(45, 99, s), lerp(70, 99, s)), 5 if not burst else 10)


def scene_hero(cv, t, lt, cfg, C):
    rise = ease(clamp(lt / 0.6))
    p = Pose(hip=(540, G - 214), lean=-4, tilt=-8, eye=1.0, brow=0.8, mouth="smile", wind=1.0, glow=0.6)
    sh = shoulder_of(p)
    p.hands = [lerp2((sh[0] + 40, sh[1] + 120), (sh[0] + 150, sh[1] - 120), rise),
               (sh[0] - 46, sh[1] + 96)]
    p.feet = [(p.hip[0] + 56, G - 14), (p.hip[0] - 50, G - 14)]
    draw_character(cv, p, C, t)
    particles(cv, t, 0.5, hexc(C["accent"]), n=30)
    caption_words(cv, cfg["lines"]["hero"].split(), 470, lt - 0.3, size=84, hl_word="Part",
                  accent=hexc(C["accent"]))
    if lt > 1.6:
        pop = ease_out_back((lt - 1.6) / 0.3)
        pulse = 1 + 0.04 * math.sin(lt * 8)
        txt = cfg["lines"]["cta"]
        sz = int(58 * max(pop, 0.05) * pulse)
        tw = cv.textlen(txt, sz)
        cv.rect((W / 2 - tw / 2 - 40, 640 - 48 * pop, W / 2 + tw / 2 + 40, 640 + 48 * pop),
                hexc(C["band"]), radius=40, outline=INK, width=6)
        cv.text((W / 2, 640), txt, sz, stroke=0)
    cv.text((W / 2, 1520), cfg["handle"], 40, fill=(255, 255, 255), stroke=6)
    part_pill(cv, cfg, y=190)


SCENE_FN = {"hook": scene_hook, "fail": scene_fail, "montage": scene_montage,
            "payoff": scene_payoff, "power": scene_power, "hero": scene_hero}


# ============================================================== frame composer

class Renderer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.C = cfg["colors"]
        self.bgs = {"hook": bg_night(), "fail": bg_night(), "montage": bg_dawn(),
                    "payoff": bg_day(), "power": None, "hero": bg_sunset()}

    def frame(self, t: float) -> Image.Image:
        name, s0, s1 = next(s for s in SCENES if s[1] <= t < s[2]) if t < TOTAL else SCENES[-1]
        lt = t - s0
        if name == "power":
            k = ease(clamp((lt - 0.3) / 3.0))
            img = gradient(mix((40, 50, 110), (8, 6, 22), k), mix((110, 120, 170), (30, 10, 50), k))
            cv = Canvas(img.convert("RGBA"))
            draw_skyline(cv, SKY_FAR, G - 120, mix((60, 70, 120), (16, 14, 36), k))
            draw_skyline(cv, SKY_NEAR, G - 60, mix((50, 55, 100), (12, 10, 28), k))
            rooftop(cv, mix((90, 90, 120), (30, 26, 50), k))
        else:
            cv = Canvas(self.bgs[name].convert("RGBA"))
        SCENE_FN[name](cv, t, lt, self.cfg, self.C)

        # progress bar
        cv.rect((0, H - 16, W * (t / TOTAL), H), hexc(self.C["accent"]))

        img = cv.img.convert("RGB").reduce(SS)

        # camera shake (power-up + face-plant)
        shake = 0.0
        if name == "power":
            shake = 10 * ease(clamp((lt - 1.0) / 2.5)) + (24 if 0 <= t - FLASH_T < 0.35 else 0)
        if name == "fail" and 2.45 <= lt < 2.75:
            shake = 18 * (1 - (lt - 2.45) / 0.3)
        if shake:
            img = ImageChops.offset(img, int(math.sin(t * 91) * shake), int(math.cos(t * 77) * shake))

        # transitions: white flash at the level-up, quick dip-to-black at other cuts
        fl = 0.0
        if 0 <= t - FLASH_T < 0.5:
            fl = 1 - (t - FLASH_T) / 0.5
        if fl > 0:
            img = Image.blend(img, Image.new("RGB", img.size, (255, 255, 255)), fl)
        for (_, a, _) in SCENES[1:]:
            d = abs(t - a)
            if d < 0.08 and a != SCENES[4][1]:
                img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), 1 - d / 0.08)
        if t < 0.3:
            img = Image.blend(Image.new("RGB", img.size, (0, 0, 0)), img, t / 0.3)
        if t > TOTAL - 0.35:
            img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), (t - (TOTAL - 0.35)) / 0.35)
        return img


# ============================================================== audio (synthesized)

SR = 44100


def env(n, a=0.005, r=0.2):
    t = np.arange(n) / SR
    e = np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / max(r, 1e-4))
    return e


def tone(freq, dur, kind="sine", a=0.01, r=0.3, vol=0.3):
    n = int(dur * SR)
    t = np.arange(n) / SR
    if kind == "saw":
        w = 2 * ((t * freq) % 1) - 1
    elif kind == "square":
        w = np.sign(np.sin(2 * np.pi * freq * t))
    else:
        w = np.sin(2 * np.pi * freq * t)
    return w * env(n, a, r) * vol


def kick(vol=0.9):
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 50 + 110 * np.exp(-t * 30)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * np.exp(-t * 9) * vol


def hat(vol=0.12):
    n = int(0.06 * SR)
    noise = np.random.default_rng(1).uniform(-1, 1, n)
    noise = np.diff(noise, prepend=0)
    return noise * env(n, 0.001, 0.015) * vol


def noise_sweep(dur, f0, f1, vol=0.3):
    n = int(dur * SR)
    rng = np.random.default_rng(2)
    x = rng.uniform(-1, 1, n)
    out = np.zeros(n)
    y = 0.0
    for i in range(0, n, 64):  # cheap one-pole lowpass with rising cutoff
        fc = f0 + (f1 - f0) * (i / n) ** 2
        a = math.exp(-2 * math.pi * fc / SR)
        seg = x[i:i + 64]
        res = np.empty_like(seg)
        for j, v in enumerate(seg):
            y = (1 - a) * v + a * y
            res[j] = y
        out[i:i + 64] = res
    return out / (np.abs(out).max() + 1e-9) * vol * np.linspace(0.1, 1, n)


def place(buf, clip, t):
    i = int(t * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(clip))
    buf[i:j] += clip[:j - i]


def note(n):  # MIDI -> Hz
    return 440 * 2 ** ((n - 69) / 12)


def soundtrack(path: Path):
    buf = np.zeros(int((TOTAL + 0.5) * SR))
    bpm = 128
    beat = 60 / bpm
    # 1) sad pad for hook + fail (A minor)
    for t0 in np.arange(0, SCENES[2][1], beat * 4):
        for m in (57, 60, 64):
            place(buf, tone(note(m), beat * 4.2, a=0.4, r=1.6, vol=0.05), t0)
    # lonely plucks
    for i, m in enumerate((76, 72, 69, 72, 76, 74)):
        place(buf, tone(note(m), 0.8, a=0.003, r=0.35, vol=0.06), 0.3 + i * beat * 2)
    # face-plant thud
    th = SCENES[1][1] + 2.45
    place(buf, kick(1.0), th)
    place(buf, tone(55, 0.6, a=0.001, r=0.25, vol=0.5), th)
    # whoosh into montage
    place(buf, noise_sweep(0.6, 300, 6000, 0.25), SCENES[2][1] - 0.5)
    # 2) driving beat montage + payoff
    bass = [45, 45, 48, 50, 45, 45, 52, 50]
    t0 = SCENES[2][1]
    i = 0
    while t0 < SCENES[4][1]:
        place(buf, kick(0.8), t0)
        place(buf, hat(), t0 + beat / 2)
        place(buf, tone(note(bass[i % 8]), beat * 0.9, "saw", a=0.005, r=0.18, vol=0.09), t0)
        if i % 2 == 0:
            for m in (69, 72, 76):
                place(buf, tone(note(m), beat * 0.4, "square", a=0.002, r=0.08, vol=0.025), t0 + beat * 0.5)
        t0 += beat
        i += 1
    # day ticks
    for d in range(2, 31):
        tt = SCENES[2][1] + 0.6 + 4.8 * (math.acos(1 - 2 * (d - 2) / 28) / math.pi)  # matches smoothstep pace roughly
        place(buf, tone(1800, 0.04, a=0.001, r=0.012, vol=0.05), tt)
    # rep blips
    for r in range(11):
        place(buf, tone(note(81 + min(r, 10)), 0.08, a=0.001, r=0.05, vol=0.06), SCENES[3][1] + 0.42 * r + 0.21)
    # 3) power-up riser + impact
    p0 = SCENES[4][1]
    place(buf, noise_sweep(FLASH_T - p0, 200, 9000, 0.35), p0)
    n = int((FLASH_T - p0) * SR)
    tt = np.arange(n) / SR
    f = np.linspace(110, 880, n)
    place(buf, np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.08 * np.linspace(0, 1, n), p0)
    place(buf, kick(1.2), FLASH_T)
    place(buf, tone(41, 1.5, a=0.001, r=0.6, vol=0.6), FLASH_T)
    place(buf, noise_sweep(0.8, 8000, 400, 0.3)[::-1], FLASH_T)
    for k, m in enumerate((72, 76, 79, 84)):  # level-up chime
        place(buf, tone(note(m), 0.5, "square", a=0.002, r=0.25, vol=0.06), FLASH_T + 0.12 + k * 0.08)
    # 4) triumphant hero theme (C major)
    t0 = SCENES[5][1]
    i = 0
    while t0 < TOTAL - 0.2:
        place(buf, kick(0.7), t0)
        place(buf, hat(), t0 + beat / 2)
        if i % 4 == 0:
            for m in (60, 64, 67, 72):
                place(buf, tone(note(m), beat * 4, "saw", a=0.05, r=1.2, vol=0.03), t0)
        t0 += beat
        i += 1
    for k, m in enumerate((72, 74, 76, 79, 76, 79, 84)):
        place(buf, tone(note(m), 0.5, a=0.005, r=0.3, vol=0.07), SCENES[5][1] + 0.3 + k * beat)

    # master: gentle limiter, fade out
    buf = np.tanh(buf * 1.4) * 0.8
    fo = int(0.5 * SR)
    buf[-fo:] *= np.linspace(1, 0, fo)
    pcm = (buf * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# ============================================================== main

def deep_merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out


def main():
    ap = argparse.ArgumentParser(description="Render the code-drawn Arc Lab Training Arc short.")
    ap.add_argument("-o", "--output", default="training-arc-part1.mp4")
    ap.add_argument("--config", help="JSON overrides for texts/colors (see DEFAULTS)")
    ap.add_argument("--still", type=float, help="render one frame at this time (s) to a PNG and exit")
    ap.add_argument("--preview", type=float, help="render only the first N seconds")
    args = ap.parse_args()

    cfg = DEFAULTS
    if args.config:
        cfg = deep_merge(DEFAULTS, json.loads(Path(args.config).read_text()))
    r = Renderer(cfg)
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.still is not None:
        r.frame(args.still).save(out.with_suffix(".png"))
        print(out.with_suffix(".png"))
        return 0
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")

    dur = min(TOTAL, args.preview or TOTAL)
    work = Path(tempfile.mkdtemp(prefix="arcanim-"))
    try:
        wav = work / "music.wav"
        print("[arc-anim] synthesizing soundtrack", file=sys.stderr)
        soundtrack(wav)
        n = int(round(dur * FPS))
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
               "-i", str(wav), "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for i in range(n):
            t = i / FPS
            proc.stdin.write(r.frame(t).tobytes())
            if i % FPS == 0:
                print(f"[arc-anim] {t:5.1f}s / {dur:.1f}s", file=sys.stderr)
        proc.stdin.close()
        if proc.wait() != 0:
            sys.exit("ffmpeg failed")
        r.frame(SCENES[5][1] + 2.5).save(out.with_name(out.stem + "_cover.png"))
        print(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Render Rocky's animation frames as RGBA PNGs. Python 3 stdlib only.

Run:  python3 -I tools/render_rocky.py [--out DIR] [--contact PATH]
                                       [--contact-small PATH]

With no arguments it overwrites assets/frames/*.png. Every directory named is
checked first, and every frame is rendered and fit-checked in memory before
anything is written. The contact sheets are written next and the frames last,
so a failure in any earlier step leaves assets/frames as it was.

Every frame is drawn at 3x into a palette-index buffer (solid scanline fills),
then box-filtered down to 240x210 with a transparent background. Limbs are
animated by joint angles; the body carries seeded, body-anchored noise. The
output is reproducible on the same platform.

The script fails if any frame's figure leaves a 4 px margin or is shorter than
75% of the canvas.
"""
import argparse
import math
import os
import struct
import sys
import zlib

W, H, SS, NFRAMES = 240, 210, 3, 12
MOODS = ('idle', 'thinking', 'working', 'worry', 'happy')
BG = (0x26, 0x26, 0x26)
PANE_FRAME = (192, 168)  # about what a frame measures in the pane

K = 1.28  # the figure's scale on the canvas
THICK = 1.5  # limbs, knuckles and fingers against the original proportions
GROUND = 195  # the canvas row the feet stand on at rest
MARGIN = 4
MIN_FILL = 0.75

# Palette indices (0 is transparent).
(OUT, BASE, DARK, HI, MID, LIMB, LIMB_BACK, SPARK,
 LIMB_SHADOW, LIMB_EDGE, BACK_SHADOW, SHADOW1, SHADOW2, SHADOW3) = range(1, 15)
COLORS = {
    OUT: (0x3A, 0x26, 0x14),
    BASE: (0xB0, 0x7A, 0x3C),
    DARK: (0x6B, 0x44, 0x23),
    HI: (0xD9, 0xA3, 0x5F),
    MID: (0x8F, 0x5E, 0x2E),
    LIMB: (0x9C, 0x6A, 0x33),
    LIMB_BACK: (0x74, 0x4E, 0x27),
    SPARK: (0xFF, 0xD2, 0x7A),
    # Limb shades, in the palette's own hue: the shadow side and the edge of a plate.
    LIMB_SHADOW: (0x7C, 0x52, 0x28),
    LIMB_EDGE: (0x4F, 0x32, 0x1A),
    BACK_SHADOW: (0x5A, 0x3B, 0x1E),
    # The contact shadow: dark, three soft rings of low alpha.
    SHADOW1: (0x08, 0x05, 0x03),
    SHADOW2: (0x08, 0x05, 0x03),
    SHADOW3: (0x08, 0x05, 0x03),
}
ALPHA = {SHADOW1: 0.14, SHADOW2: 0.20, SHADOW3: 0.30}
BODY_MATS = {BASE, DARK, HI, MID}

def h01(*vals):
    """A deterministic hash of some integers, in [0, 1)."""
    h = 0x9E3779B9
    for v in vals:
        h = ((h ^ (int(v) & 0xFFFFFFFF)) * 0x85EBCA6B) & 0xFFFFFFFF
        h ^= h >> 15
    return (h & 0xFFFFFF) / 0x1000000


# A faceted rock, wider than tall, relative to the body centre (y down). The
# silhouette is uneven, with two chips bitten out of it.
SHELL = [(-47, -6), (-45, -20), (-35, -30), (-24, -33), (-12, -38), (2, -36), (10, -35), (13, -30),
         (20, -36), (32, -31), (43, -16), (47, -2), (45, 12), (38, 25), (30, 33), (16, 31), (8, 37),
         (-6, 38), (-18, 35), (-30, 31), (-40, 24), (-45, 10)]
# Cracks: jagged lines running in from the edge.
CRACKS = [[(-35, -30), (-28, -19), (-31, -10), (-22, -2)],
          [(30, 33), (24, 22), (28, 13)],
          [(10, -35), (6, -23), (11, -14), (5, -5)],
          [(-40, 24), (-31, 17), (-33, 8)]]
# Pits: small dark nicks in the faces.
PITS = [[(-18, -14), (-14, -16), (-15, -11)], [(18, 8), (23, 6), (21, 12)], [(-8, 14), (-3, 12), (-5, 18)]]


def build_facets():
    """Triangles of a faceted dome (an outer ring, an inner ring, a centre), each
    lit from the top left and sorted by brightness into four tones."""
    n = len(SHELL)
    inner = []
    for i, (x, y) in enumerate(SHELL):
        r = 0.34 + 0.34 * h01(i, 11)
        inner.append((x * r + (h01(i, 12) - 0.5) * 6, y * r + (h01(i, 13) - 0.5) * 6))
    h_out = [1.5 * h01(i, 21) for i in range(n)]
    h_in = [9 + 8 * h01(i, 22) for i in range(n)]
    c, hc = (2, -3), 17
    tris = []
    for i in range(n):
        j = (i + 1) % n
        tris.append([(SHELL[i], h_out[i]), (SHELL[j], h_out[j]), (inner[i], h_in[i])])
        tris.append([(SHELL[j], h_out[j]), (inner[j], h_in[j]), (inner[i], h_in[i])])
        tris.append([(c, hc), (inner[i], h_in[i]), (inner[j], h_in[j])])
    lx, ly, lz = -0.5, -0.62, 0.6
    ln = math.sqrt(lx * lx + ly * ly + lz * lz)
    lits = []
    for t, tri in enumerate(tris):
        (ax, ay), ah = tri[0]
        (bx, by), bh = tri[1]
        (cx, cy), ch = tri[2]
        ux, uy, uz = bx - ax, by - ay, bh - ah
        vx, vy, vz = cx - ax, cy - ay, ch - ah
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        if nz < 0:
            nx, ny, nz = -nx, -ny, -nz
        nl = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        lit = (nx * lx + ny * ly + nz * lz) / (nl * ln)
        mx, my = (ax + bx + cx) / 3, (ay + by + cy) / 3
        lits.append(lit + (h01(t, 31) - 0.5) * 0.25 - 0.004 * mx - 0.005 * my)
    order = sorted(range(len(tris)), key=lambda t: lits[t])
    tone = {}
    for rank, t in enumerate(order):
        q = rank / len(order)
        tone[t] = DARK if q < 0.26 else MID if q < 0.56 else BASE if q < 0.82 else HI
    return [([pt for pt, _ in tri], tone[t]) for t, tri in enumerate(tris)]


FACETS = build_facets()

SHOULDER = {'L': (-42, -14), 'R': (42, -14)}
HIP = {'L': (-24, 26), 'R': (26, 26), 'B': (-12, 28)}
ARM_LEN = (20, 17)
LEG_LEN = {'L': (28, 26), 'R': (28, 26), 'B': (25, 23)}
# Where the sole of the rest-pose feet sits, relative to the body centre.
FOOT_REL = 26 + 28 * math.cos(math.radians(28)) + 26 * math.cos(math.radians(6)) + 3


def lerp(a, b, t):
    return a + (b - a) * t


def rad(deg):
    return math.radians(deg)


# --------------------------------------------------------------------------
# Rasteriser: palette-index buffer at SS x resolution.


class Canvas:
    def __init__(self):
        self.w, self.h = W * SS, H * SS
        self.rows = [bytearray(self.w) for _ in range(self.h)]

    def poly(self, pts, idx):
        """Even-odd scanline fill of a polygon given in 1x coordinates."""
        p = [(x * SS, y * SS) for x, y in pts]
        n = len(p)
        ys = [y for _, y in p]
        y0 = max(0, int(math.floor(min(ys))))
        y1 = min(self.h - 1, int(math.ceil(max(ys))))
        fill = bytes([idx])
        for y in range(y0, y1 + 1):
            yc = y + 0.5
            xs = []
            for k in range(n):
                ax, ay = p[k]
                bx, by = p[(k + 1) % n]
                if (ay <= yc < by) or (by <= yc < ay):
                    xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
            xs.sort()
            row = self.rows[y]
            for k in range(0, len(xs) - 1, 2):
                xa = max(0, int(math.floor(xs[k] + 0.5)))
                xb = min(self.w, int(math.floor(xs[k + 1] + 0.5)))
                if xb > xa:
                    row[xa:xb] = fill * (xb - xa)

    def circle(self, c, r, idx):
        self.poly([(c[0] + r * math.cos(a), c[1] + r * math.sin(a)) for a in (k * math.pi / 8 for k in range(16))], idx)

    def taper(self, a, b, wa, wb, idx):
        """A tapered segment from a to b, half-widths wa and wb, round ends."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / d, dx / d
        self.poly([(a[0] + nx * wa, a[1] + ny * wa), (b[0] + nx * wb, b[1] + ny * wb),
                   (b[0] - nx * wb, b[1] - ny * wb), (a[0] - nx * wa, a[1] - ny * wa)], idx)
        self.circle(a, wa, idx)
        self.circle(b, wb, idx)

    def line(self, a, b, w, idx):
        self.taper(a, b, w / 2, w / 2, idx)


# --------------------------------------------------------------------------
# The figure.


class Xf:
    """Body transform: scale K, offset, plus squash about the bottom of the shell."""

    def __init__(self, cx, cy, sx=1.0, sy=1.0):
        self.cx, self.cy, self.sx, self.sy = cx, cy, sx, sy

    def __call__(self, p):
        return (self.cx + K * self.sx * p[0], self.cy + K * 38 + K * self.sy * (p[1] - 38))


def draw_body(cv, xf):
    for pts, tone in FACETS:
        cv.poly([xf(p) for p in pts], tone)
    for line in CRACKS:
        for a, b in zip(line, line[1:]):
            cv.line(xf(a), xf(b), 1.5 * K, DARK)
    for pit in PITS:
        cv.poly([xf(p) for p in pit], DARK)
    poly = [xf(v) for v in SHELL]
    for k in range(len(poly)):
        cv.line(poly[k], poly[(k + 1) % len(poly)], 3.0 * K, OUT)


def limb_points(root, t1, t2, lens):
    j = (root[0] + K * lens[0] * math.sin(rad(t1)), root[1] + K * lens[0] * math.cos(rad(t1)))
    e = (j[0] + K * lens[1] * math.sin(rad(t2)), j[1] + K * lens[1] * math.cos(rad(t2)))
    return j, e


LIGHT = (-0.6, -0.8)  # towards the light, in the picture plane


def edges(a, b, wa, wb, seed, samples=4):
    """The two long edges of a tapered segment, each a few points, slightly
    uneven so the segment reads as a faceted plate. Returns (axis, +normal side,
    -normal side, the unit normal)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    d = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / d, dx / d
    axis, pos, neg = [], [], []
    for i in range(samples):
        t = i / (samples - 1)
        cx, cy = a[0] + dx * t, a[1] + dy * t
        w = lerp(wa, wb, t)
        jp = 1 + (h01(seed, i, 1) - 0.5) * 0.22 if 0 < i < samples - 1 else 1
        jn = 1 + (h01(seed, i, 2) - 0.5) * 0.22 if 0 < i < samples - 1 else 1
        axis.append((cx, cy))
        pos.append((cx + nx * w * jp, cy + ny * w * jp))
        neg.append((cx - nx * w * jn, cy - ny * w * jn))
    return axis, pos, neg, (nx, ny)


def draw_plate(cv, a, b, wa, wb, seed, light, shadow, edge):
    """A limb segment: a lit face, a shadow side, a dark edge, and a seam."""
    axis, pos, neg, (nx, ny) = edges(a, b, wa, wb, seed)
    cv.poly(pos + neg[::-1], light)
    # the side facing away from the light is the shadow side
    shade = neg if nx * LIGHT[0] + ny * LIGHT[1] >= 0 else pos
    cv.poly(axis + shade[::-1], shadow)
    for p, q in zip(shade, shade[1:]):
        cv.line(p, q, 1.3 * K, edge)
    m = 0.5
    mid_a = (lerp(pos[1][0], pos[2][0], m), lerp(pos[1][1], pos[2][1], m))
    mid_b = (lerp(neg[1][0], neg[2][0], m), lerp(neg[1][1], neg[2][1], m))
    cv.line(mid_a, mid_b, 1.0 * K, edge)


def draw_knob(cv, c, r, seed, light, shadow, grow=0.0):
    """A knobby rock knuckle: an uneven polygon with a shadow facet and a glint."""
    pts = []
    for i in range(9):
        a = 2 * math.pi * i / 9
        rr = (r * (0.84 + 0.3 * h01(seed, i, 5))) + grow
        pts.append((c[0] + rr * math.cos(a), c[1] + rr * math.sin(a)))
    if grow:
        cv.poly(pts, OUT)
        return
    cv.poly(pts, light)
    cv.poly([c] + pts[1:6], shadow)  # the lower right, away from the light
    g = [(c[0] - r * 0.55, c[1] - r * 0.5), (c[0] - r * 0.1, c[1] - r * 0.62), (c[0] - r * 0.45, c[1] - r * 0.1)]
    cv.poly(g, HI)


def tip_polygon(end, t2, ground, u):
    """A blunt, slightly flattened rock end that continues the limb: tapered,
    flat across the end, corners chamfered. A leg's end is flat to the ground.
    Returns (polygon, tip centre, the end edge's left and right corners)."""
    dv = (math.sin(rad(t2)), math.cos(rad(t2)))
    n = (-dv[1], dv[0])
    t = (end[0] + dv[0] * 4.6 * u, end[1] + dv[1] * 4.6 * u)
    w0, w1 = 2.6 * u, 2.3 * u
    left0 = (end[0] + n[0] * w0, end[1] + n[1] * w0)
    right0 = (end[0] - n[0] * w0, end[1] - n[1] * w0)
    if ground:
        corners = [(t[0] - w1, t[1]), (t[0] + w1, t[1])]
        corners.sort(key=lambda q: -(n[0] * (q[0] - t[0]) + n[1] * (q[1] - t[1])))
        bl, br = corners
    else:
        bl = (t[0] + n[0] * w1, t[1] + n[1] * w1)
        br = (t[0] - n[0] * w1, t[1] - n[1] * w1)
    c = 1.4 * u
    pts = [left0,
           (bl[0] - dv[0] * c, bl[1] - dv[1] * c),
           (bl[0] + (t[0] - bl[0]) * 0.3, bl[1] + (t[1] - bl[1]) * 0.3),
           (br[0] + (t[0] - br[0]) * 0.3, br[1] + (t[1] - br[1]) * 0.3),
           (br[0] - dv[0] * c, br[1] - dv[1] * c),
           right0]
    return pts, t, bl, br, dv, n


def draw_limb(cv, root, joint, end, t2, ground, fill, seed):
    """Two tapered plates and a blunt end, with knobby knuckles at the joint and
    the root. Arms and legs are the same kind of limb: only the pose differs.
    Returns where the blunt end finishes."""
    u = K * THICK
    shadow = BACK_SHADOW if fill == LIMB_BACK else LIMB_SHADOW
    edge = LIMB_EDGE
    w_root, w_joint, w_end = 4.6 * u, 3.8 * u, 2.6 * u
    tip, t, bl, br, dv, n = tip_polygon(end, t2, ground, u)
    grow = 1.7 * K

    # outlines first, so a plate's outline never cuts into the plate before it
    cv.taper(root, joint, w_root + grow, w_joint + grow, OUT)
    cv.taper(joint, end, w_joint + grow, w_end + grow, OUT)
    cv.poly(tip, OUT)
    for p, q in zip(tip, tip[1:] + tip[:1]):
        cv.line(p, q, 2 * grow, OUT)
    draw_knob(cv, joint, 4.8 * u, seed + 1, fill, shadow, grow=grow)
    draw_knob(cv, root, 4.4 * u, seed + 2, fill, shadow, grow=grow)

    draw_plate(cv, root, joint, w_root, w_joint, seed + 3, fill, shadow, edge)
    draw_plate(cv, joint, end, w_joint, w_end, seed + 4, fill, shadow, edge)
    # the blunt end: lit face, shadow side, a crease across it, a notch in its end
    cv.poly(tip, fill)
    lit_left = n[0] * LIGHT[0] + n[1] * LIGHT[1] >= 0
    if lit_left:  # the +n side (left0 .. bl) is lit: shadow is the other half
        half = [tip[5], tip[4], tip[3], (end[0], end[1])]
        side = [tip[4], tip[5]]
    else:
        half = [tip[0], tip[1], tip[2], (end[0], end[1])]
        side = [tip[0], tip[1]]
    cv.poly(half, shadow)
    cv.line(side[0], side[1], 1.3 * K, edge)
    ca = ((tip[0][0] + tip[1][0]) / 2, (tip[0][1] + tip[1][1]) / 2)
    cb = ((tip[4][0] + tip[5][0]) / 2, (tip[4][1] + tip[5][1]) / 2)
    cv.line(lerp_pt(ca, cb, 0.0), lerp_pt(ca, cb, 0.8), 1.0 * K, edge)  # a crease across the end
    e = ((br[0] - bl[0]), (br[1] - bl[1]))
    el = math.hypot(*e) or 1.0
    ev = (e[0] / el, e[1] / el)
    nw = 0.5 * u
    cv.poly([(t[0] - ev[0] * nw, t[1] - ev[1] * nw), (t[0] + ev[0] * nw, t[1] + ev[1] * nw),
             (t[0] - dv[0] * 1.5 * u, t[1] - dv[1] * 1.5 * u)], OUT)  # a notch in the end
    draw_knob(cv, joint, 4.8 * u, seed + 1, fill, shadow)
    draw_knob(cv, root, 4.4 * u, seed + 2, fill, shadow)
    return t


def lerp_pt(a, b, t):
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


def spark(cv, at, k):
    """A small burst of bright lines at the low hand; k in 0..1 is its size."""
    for dx, dy in ((-0.9, -0.4), (-0.4, -0.9), (0.4, -0.9), (0.9, -0.4)):
        a = (at[0] + dx * 6 * K, at[1] + dy * 6 * K)
        b = (at[0] + dx * (7 + 10 * k) * K, at[1] + dy * (7 + 10 * k) * K)
        cv.line(a, b, 2.6 * K, SPARK)


def contact_shadow(cv, cx, lift):
    """A soft contact shadow under the feet: three low-alpha rings. It narrows
    as he lifts off."""
    f = max(0.6, 1 - lift * 0.025)
    cy = GROUND + 3
    for rx, ry, idx in ((64, 6.5, SHADOW1), (50, 5, SHADOW2), (34, 3.2, SHADOW3)):
        cv.poly([(cx + rx * f * math.cos(a), cy + ry * math.sin(a))
                 for a in (k * math.pi / 12 for k in range(24))], idx)


# --------------------------------------------------------------------------
# Poses: every angle is a periodic function of phase, so each loop is seamless.


def ease(x):
    x = max(0.0, min(1.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def hammer(u):
    """0 = arm up, 1 = arm down: a quick accelerating strike, a slower eased lift."""
    u %= 1.0
    if u < 0.3:
        return (u / 0.3) ** 2
    return 1 - ease((u - 0.3) / 0.7)


def pose(mood, i):
    t = i / NFRAMES
    ph = 2 * math.pi * t
    s, c = math.sin(ph), math.cos(ph)
    # Rest pose angles; the right side mirrors the left by negating.
    arm = {'L': [-26.0, -12.0], 'R': [26.0, 12.0]}  # low and relaxed, a little outward
    leg = {'L': [-28.0, -6.0], 'R': [28.0, 6.0], 'B': [-72.0, -38.0]}
    p = {'dx': 0.0, 'dy': 0.0, 'sx': 1.0, 'sy': 1.0, 'sparks': []}

    if mood == 'idle':
        p['dy'] = 1 - c  # a breath: 0..2 px
        arm['L'] = [-26 + 4 * s, -12 + 4 * s]
        arm['R'] = [26 - 4 * s, 12 - 4 * s]
    elif mood == 'thinking':
        # Left arm rests low; the right hand comes up to the top of the shell.
        tap = 3 * math.sin(4 * ph)  # two small taps per loop
        arm['L'] = [-26.0, -12.0 + tap]
        arm['R'] = [140 + 2 * s, -125 - 2 * s]
        p['dy'] = 0.8 * (1 - c)
    elif mood == 'working':
        for key, sign, off in (('L', -1, 0.0), ('R', 1, 0.5)):
            k = hammer(2 * t + off)  # two strikes per arm per loop, the arms alternating
            arm[key] = [sign * lerp(130, 35, k), sign * (lerp(160, -5, k) - 75 * math.sin(math.pi * k))]  # the forearm folds in mid-travel
            if k > 0.9:
                p['sparks'].append((key, (k - 0.9) / 0.1))
        p['dy'] = 2.0 * max(hammer(2 * t), hammer(2 * t + 0.5)) ** 4
    elif mood == 'worry':
        p['dx'] = 2 * math.sin(4 * ph)  # fast, small shake
        arm['L'] = [-25 + 3 * math.sin(4 * ph), 55.0]
        arm['R'] = [25 - 3 * math.sin(4 * ph), -55.0]
        sh = 8 * math.sin(2 * ph)
        leg['L'] = [-28 + sh, -6 + sh]
        leg['R'] = [28 + sh, 6 + sh]
        leg['B'] = [-72 - sh, -38 - sh]
    elif mood == 'happy':
        lift = 6 * math.sin(math.pi * t)  # one bounce per loop, landing at t = 0
        land = math.exp(-((lift / 1.4) ** 2))
        p['dy'] = -lift
        p['sy'] = 1 - 0.10 * land
        p['sx'] = 1 + 0.10 * land
        wave = 12 * math.sin(2 * ph)
        arm['L'] = [-125 + 0.5 * wave, -172 + 1.6 * wave]
        arm['R'] = [125 + 0.5 * wave, 172 + 1.6 * wave]
    return arm, leg, p


# --------------------------------------------------------------------------
# One frame.


def render_frame(mood, i):
    arm, leg, p = pose(mood, i)
    cv = Canvas()
    xf = Xf(120 + K * p['dx'], GROUND - K * FOOT_REL + K * p['dy'], p['sx'], p['sy'])

    contact_shadow(cv, 120 + K * p['dx'] * 0.5, -p['dy'])

    # Legs sit behind the body, the back leg furthest and darker; each rests its
    # blunt end on the ground.
    for key, fill, seed in (('B', LIMB_BACK, 100), ('L', LIMB, 200), ('R', LIMB, 300)):
        root = xf(HIP[key])
        t1, t2 = leg[key]
        j, e = limb_points(root, t1, t2, LEG_LEN[key])
        draw_limb(cv, root, j, e, t2, True, fill, seed)

    draw_body(cv, xf)

    hands = {}
    for key, seed in (('L', 400), ('R', 500)):
        root = xf(SHOULDER[key])
        t1, t2 = arm[key]
        j, e = limb_points(root, t1, t2, ARM_LEN)
        hands[key] = draw_limb(cv, root, j, e, t2, False, LIMB, seed)
    for key, k in p['sparks']:
        spark(cv, hands[key], k)

    return downsample(cv, (round(K * p['dx']), round(K * p['dy'])))


def noise(x, y):
    """Stone grain: fine per-pixel grain, 2 px speckle, a few dark pits and
    light flecks. Roughly -1.5 .. 1.2."""
    fine = h01(x, y, 1) - 0.5
    coarse = h01(x // 2, y // 2, 2) - 0.5
    r = h01(x, y, 3)
    pit = -1.1 if r < 0.04 else (0.8 if r > 0.985 else 0.0)
    return fine * 0.9 + coarse * 0.8 + pit


def downsample(cv, anchor):
    """Box filter 3x3 -> 1x, RGBA. Body pixels get grain fixed to the body."""
    cache = {}
    out = bytearray(W * H * 4)
    ox, oy = anchor
    k = 0
    for y in range(H):
        r0, r1, r2 = cv.rows[y * SS], cv.rows[y * SS + 1], cv.rows[y * SS + 2]
        for x in range(W):
            a = x * SS
            key = bytes(r0[a:a + 3] + r1[a:a + 3] + r2[a:a + 3])
            hit = cache.get(key)
            if hit is None:
                rs = gs = bs = cover = 0.0
                for v in key:
                    if v:
                        al = ALPHA.get(v, 1.0)
                        col = COLORS[v]
                        rs += col[0] * al
                        gs += col[1] * al
                        bs += col[2] * al
                        cover += al
                if cover == 0:
                    hit = (0, 0, 0, 0, False)
                else:
                    body = all(v in BODY_MATS for v in key)
                    hit = (int(rs / cover + 0.5), int(gs / cover + 0.5), int(bs / cover + 0.5),
                           int(255 * cover / 9 + 0.5), body)
                cache[key] = hit
            r, g, b, al, body = hit
            if body:
                d = int(noise(x - ox, y - oy) * 15)
                r, g, b = (min(255, max(0, v + d)) for v in (r, g, b))
            out[k:k + 4] = bytes((r, g, b, al))
            k += 4
    return bytes(out)


def bounds(rgba):
    """The box (x0, y0, x1, y1) of the non-transparent pixels, inclusive."""
    x0, y0, x1, y1 = W, H, -1, -1
    for y in range(H):
        row = rgba[y * W * 4:(y + 1) * W * 4]
        xs = [x for x in range(W) if row[x * 4 + 3]]
        if xs:
            y0, y1 = min(y0, y), max(y1, y)
            x0, x1 = min(x0, xs[0]), max(x1, xs[-1])
    return x0, y0, x1, y1


def check_fit(mood, i, rgba):
    x0, y0, x1, y1 = bounds(rgba)
    problems = []
    if x1 < 0:
        problems.append('is empty')
    else:
        if min(x0, y0, W - 1 - x1, H - 1 - y1) < MARGIN:
            problems.append(f'leaves less than {MARGIN}px margin: box {x0},{y0}..{x1},{y1}')
        if (y1 - y0 + 1) < MIN_FILL * H:
            problems.append(f'is {y1 - y0 + 1}px tall, under {MIN_FILL:.0%} of {H}')
    if problems:
        raise SystemExit(f'frame {mood}-{i:02d} ' + '; '.join(problems))
    return x0, y0, x1, y1


# --------------------------------------------------------------------------
# PNG output and contact sheets.


def chunk(tag, data):
    body = tag + data
    return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)


def png_bytes(width, height, rgba):
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
            + chunk(b'IEND', b''))


def write_png(path, width, height, rgba):
    data = png_bytes(width, height, rgba)
    with open(path, 'wb') as f:
        f.write(data)
    return len(data)


def resize(rgba, sw, sh, dw, dh):
    """Area-average resample of straight-alpha RGBA (premultiplied inside)."""
    out = bytearray(dw * dh * 4)
    for y in range(dh):
        ya, yb = y * sh / dh, (y + 1) * sh / dh
        for x in range(dw):
            xa, xb = x * sw / dw, (x + 1) * sw / dw
            r = g = b = a = wsum = 0.0
            for yy in range(int(ya), min(sh, int(math.ceil(yb)))):
                wy = min(yy + 1, yb) - max(yy, ya)
                for xx in range(int(xa), min(sw, int(math.ceil(xb)))):
                    w = wy * (min(xx + 1, xb) - max(xx, xa))
                    s = (yy * sw + xx) * 4
                    al = rgba[s + 3] / 255.0
                    r += rgba[s] * al * w
                    g += rgba[s + 1] * al * w
                    b += rgba[s + 2] * al * w
                    a += al * w
                    wsum += w
            o = (y * dw + x) * 4
            if a > 0:
                out[o:o + 3] = bytes(min(255, int(v / a + 0.5)) for v in (r, g, b))
                out[o + 3] = min(255, int(a / wsum * 255 + 0.5))
    return bytes(out)


def contact_sheet(frames, path, size):
    """One row per mood, every frame side by side, on the pane's dark grey."""
    fw, fh = size
    sw, sh = fw * NFRAMES, fh * len(MOODS)
    sheet = bytearray(bytes(BG + (255,)) * (sw * sh))
    for r, mood in enumerate(MOODS):
        for i, rgba in enumerate(frames[mood]):
            if size != (W, H):
                rgba = resize(rgba, W, H, fw, fh)
            for y in range(fh):
                base = ((r * fh + y) * sw + i * fw) * 4
                for x in range(fw):
                    s = (y * fw + x) * 4
                    a = rgba[s + 3]
                    if a == 0:
                        continue
                    o = base + x * 4
                    if a == 255:
                        sheet[o:o + 3] = rgba[s:s + 3]
                    else:
                        for ch in range(3):
                            sheet[o + ch] = (rgba[s + ch] * a + sheet[o + ch] * (255 - a)) // 255
    return write_png(path, sw, sh, bytes(sheet)), (sw, sh)


def check_dir(parser, flag, path, make=False):
    """The directory a flag writes into must exist (or, for --out, be creatable)."""
    d = path if make else (os.path.dirname(os.path.abspath(path)) or '.')
    if os.path.exists(d):
        if not os.path.isdir(d):
            parser.error(f'{flag}: {d} is not a directory')
    elif make:
        parent = os.path.dirname(os.path.abspath(d))
        if not os.path.isdir(parent):
            parser.error(f'{flag}: neither {d} nor its parent exists')
    else:
        parser.error(f'{flag}: the directory {d} does not exist')
    if os.path.isdir(d) and not os.access(d, os.W_OK):
        parser.error(f'{flag}: {d} is not writable')


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Render Rocky's frames as PNGs.")
    ap.add_argument('--out', default=os.path.join(here, '..', 'assets', 'frames'),
                    help='directory for <mood>-<nn>.png (default: assets/frames)')
    ap.add_argument('--contact', help='write a full-size contact sheet here')
    ap.add_argument('--contact-small', help='write a pane-size contact sheet here')
    args = ap.parse_args(argv)

    check_dir(ap, '--out', args.out, make=True)
    for flag, path in (('--contact', args.contact), ('--contact-small', args.contact_small)):
        if path:
            check_dir(ap, flag, path)

    # Render and check everything first: a violation writes nothing.
    frames = {}
    lo, hi = H, 0
    for mood in MOODS:
        frames[mood] = []
        for i in range(NFRAMES):
            rgba = render_frame(mood, i)
            _, y0, _, y1 = check_fit(mood, i, rgba)
            lo, hi = min(lo, y1 - y0 + 1), max(hi, y1 - y0 + 1)
            frames[mood].append(rgba)

    pngs = {mood: [png_bytes(W, H, rgba) for rgba in frames[mood]] for mood in MOODS}
    print(f'fit ok: all {len(MOODS) * NFRAMES} frames keep a {MARGIN}px margin; '
          f'figure height {lo}..{hi}px of {H} ({lo / H:.0%}..{hi / H:.0%}, floor {MIN_FILL:.0%})')
    # The contact sheets go first: if one cannot be written, assets/frames is
    # left as it was.
    if args.contact:
        n, dims = contact_sheet(frames, args.contact, (W, H))
        print(f'contact sheet: {args.contact} {dims[0]}x{dims[1]}, {n} bytes')
    if args.contact_small:
        n, dims = contact_sheet(frames, args.contact_small, PANE_FRAME)
        print(f'small contact sheet: {args.contact_small} {dims[0]}x{dims[1]}, {n} bytes')

    os.makedirs(args.out, exist_ok=True)
    total = 0
    for mood in MOODS:
        for i, data in enumerate(pngs[mood]):
            with open(os.path.join(args.out, f'{mood}-{i:02d}.png'), 'wb') as f:
                f.write(data)
        size = sum(len(d) for d in pngs[mood])
        total += size
        print(f'{mood}: {NFRAMES} frames, {size} bytes')
    print(f'total: {total} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

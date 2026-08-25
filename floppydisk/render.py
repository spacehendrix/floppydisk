"""Rendering: colors, the ASCII sunburst map, bars, and artwork."""

import bisect
import curses
import math

TAU = math.tau

# ---------------------------------------------------------------- colors

NHUES = 12
# Rainbow-ordered (dim, bright) xterm-256 pairs so angle maps to a smooth wheel.
_HUES_256 = [
    (196, 203), (202, 209), (208, 215), (214, 221),
    (184, 227), (76, 119), (42, 84), (45, 87),
    (39, 81), (63, 105), (129, 171), (198, 212),
]

PAIR_SEL = 25
PAIR_FREE = 26
PAIR_HEAD = 27
PAIR_DIM = 28
PAIR_WARN = 29
PAIR_OK = 30
PAIR_TEXT = 31
PAIR_BAR = 32


def init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    if curses.COLORS >= 256:
        for i, (dim, bright) in enumerate(_HUES_256):
            curses.init_pair(1 + i * 2, dim, bg)
            curses.init_pair(2 + i * 2, bright, bg)
        curses.init_pair(PAIR_SEL, 231, bg)
        curses.init_pair(PAIR_FREE, 240, bg)
        curses.init_pair(PAIR_HEAD, 117, bg)
        curses.init_pair(PAIR_DIM, 245, bg)
        curses.init_pair(PAIR_WARN, 203, bg)
        curses.init_pair(PAIR_OK, 84, bg)
        curses.init_pair(PAIR_TEXT, 252, bg)
        curses.init_pair(PAIR_BAR, 75, bg)
    else:
        base = [curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_MAGENTA]
        for i in range(NHUES):
            c = base[i % len(base)]
            curses.init_pair(1 + i * 2, c, bg)
            curses.init_pair(2 + i * 2, c, bg)
        curses.init_pair(PAIR_SEL, curses.COLOR_WHITE, bg)
        curses.init_pair(PAIR_FREE, curses.COLOR_WHITE, bg)
        curses.init_pair(PAIR_HEAD, curses.COLOR_CYAN, bg)
        curses.init_pair(PAIR_DIM, curses.COLOR_WHITE, bg)
        curses.init_pair(PAIR_WARN, curses.COLOR_RED, bg)
        curses.init_pair(PAIR_OK, curses.COLOR_GREEN, bg)
        curses.init_pair(PAIR_TEXT, curses.COLOR_WHITE, bg)
        curses.init_pair(PAIR_BAR, curses.COLOR_CYAN, bg)


def hue_pair_no(hue, shade):
    return 1 + (hue % NHUES) * 2 + (shade & 1)


# ------------------------------------------------------------- segments

MIN_FRAC = 0.004  # slices thinner than this fraction of the circle are pooled


def build_segments(root, max_depth, free_bytes=0, selected=None):
    """Compute sunburst arcs for `root`.

    Returns (rings, starts, cmap):
      rings[d] = list of (a0, a1, pair_no, highlighted) sorted by a0
      starts[d] = parallel list of a0 for bisect lookup
      cmap = {child_node: pair_no} for root's direct children (list bullets)
    """
    rings = [[] for _ in range(max_depth)]
    cmap = {}
    total = root.size + max(0, free_bytes)
    if total <= 0:
        return rings, [[] for _ in rings], cmap

    def place(node, a0, a1, depth, hue, hl):
        kids = node.children
        if not kids or depth >= max_depth or node.size <= 0:
            return
        kids = sorted(kids, key=lambda c: c.size, reverse=True)
        acc = a0
        span = a1 - a0
        for i, ch in enumerate(kids):
            w = span * (ch.size / node.size)
            if w / TAU < MIN_FRAC:
                rest = a1 - acc
                if rest / TAU >= MIN_FRAC:
                    rings[depth].append((acc, a1, PAIR_DIM, hl))
                break
            h = hue if depth else int(((acc + w / 2) / TAU) * NHUES) % NHUES
            pair = hue_pair_no(h, depth + i)
            chl = hl or (ch is selected)
            if depth == 0:
                cmap[ch] = pair
            rings[depth].append((acc, acc + w, pair, chl))
            if ch.is_dir:
                place(ch, acc, acc + w, depth + 1, h, chl)
            acc += w

    used_end = TAU * (root.size / total)
    place(root, 0.0, used_end, 0, 0, False)
    if total > root.size and (TAU - used_end) / TAU > 0.001:
        rings[0].append((used_end, TAU, PAIR_FREE, False))
    starts = [[s[0] for s in ring] for ring in rings]
    return rings, starts, cmap


def draw_sunburst(scr, y0, x0, h, w, rings, starts, center_lines):
    """Paint the sunburst into the rectangle (y0, x0, h, w) of scr."""
    cy = y0 + h / 2.0
    cx = x0 + w / 2.0
    radius = min(w / 2.0 - 1, h - 1.0)  # dy is doubled for cell aspect
    if radius < 7:
        safe_addstr(scr, int(cy), x0 + 1, "(window too small)",
                    curses.color_pair(PAIR_DIM))
        return
    max_depth = len(rings)
    r0 = radius * 0.34
    rw = (radius - r0) / max_depth
    for y in range(y0, y0 + h):
        dy = (y + 0.5 - cy) * 2.0
        for x in range(x0, x0 + w):
            dx = x + 0.5 - cx
            r = math.hypot(dx, dy)
            if r < r0 or r >= radius:
                continue
            d = int((r - r0) / rw)
            if d >= max_depth:
                continue
            ring = rings[d]
            if not ring:
                continue
            ang = math.atan2(dx, -dy) % TAU
            i = bisect.bisect_right(starts[d], ang) - 1
            if i < 0:
                continue
            seg = ring[i]
            if ang >= seg[1]:
                continue
            attr = curses.color_pair(seg[2])
            if seg[3]:
                safe_addstr(scr, y, x, "▒", attr | curses.A_BOLD)
            else:
                safe_addstr(scr, y, x, "█", attr)
    # center hole text
    ty = int(cy - len(center_lines) / 2)
    for i, (line, attr) in enumerate(center_lines):
        maxw = max(6, int(r0 * 2) - 2)
        line = truncate(line, maxw)
        safe_addstr(scr, ty + i, int(cx - len(line) / 2), line, attr)


# ------------------------------------------------------------------ misc

EIGHTHS = " ▏▎▍▌▋▊▉█"


def bar_str(frac, width):
    frac = max(0.0, min(1.0, frac))
    cells = frac * width
    full = int(cells)
    rem = int((cells - full) * 8)
    s = "█" * full
    if full < width and rem:
        s += EIGHTHS[rem]
    return s.ljust(width)


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or u == "PB":
            if u == "B":
                return "%d B" % int(n)
            return ("%.2f %s" if n < 10 else "%.1f %s") % (n, u)
        n /= 1000.0


def fmt_time(secs):
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    return "%dm %02ds" % (secs // 60, secs % 60)


def truncate(s, width, head=False):
    if width <= 1:
        return s[:max(0, width)]
    if len(s) <= width:
        return s
    if head:  # keep the tail (useful for paths)
        return "…" + s[-(width - 1):]
    return s[:width - 1] + "…"


def safe_addstr(scr, y, x, s, attr=0):
    if y < 0 or x < 0:
        return
    maxy, maxx = scr.getmaxyx()
    if y >= maxy or x >= maxx:
        return
    s = s[: maxx - x]
    try:
        scr.addstr(y, x, s, attr)
    except curses.error:
        pass  # writing the bottom-right corner always raises


def draw_box(scr, y, x, h, w, title=""):
    safe_addstr(scr, y, x, "┌" + "─" * (w - 2) + "┐")
    for i in range(1, h - 1):
        safe_addstr(scr, y + i, x, "│" + " " * (w - 2) + "│")
    safe_addstr(scr, y + h - 1, x, "└" + "─" * (w - 2) + "┘")
    if title:
        safe_addstr(scr, y, x + 2, " %s " % title,
                    curses.color_pair(PAIR_HEAD) | curses.A_BOLD)


# ------------------------------------------------------------------- art

LOGO = [
    "╔═╗╦  ╔═╗╔═╗╔═╗╦ ╦╔╦╗╦╔═╗╦╔═",
    "╠╣ ║  ║ ║╠═╝╠═╝╚╦╝ ║║║╚═╗╠╩╗",
    "╚  ╩═╝╚═╝╩  ╩   ╩ ═╩╝╩╚═╝╩ ╩",
]

FLOPPY = [
    "┌────────────────┐",
    "│░░┌──────┐░░░▐▌░│",
    "│░░│      │░░░░░░│",
    "│░░└──────┘░░░░░░│",
    "│░░░░░░░░░░░░░░░░│",
    "│┌──────────────┐│",
    "││ floppydisk   ││",
    "││ 1.44 ZB HD   ││",
    "││              ││",
    "└┴──────────────┴┘",
]

FLOPPY_LED = "▐▌"  # replace with ░░ to blink the activity light off

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

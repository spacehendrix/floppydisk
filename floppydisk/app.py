"""The floppydisk TUI: volume picker, scan screen, and sunburst browser."""

import curses
import os
import subprocess
import time

from . import render
from .render import (
    PAIR_BAR, PAIR_DIM, PAIR_FREE, PAIR_HEAD, PAIR_OK, PAIR_SEL, PAIR_TEXT,
    PAIR_WARN, bar_str, draw_box, fmt_time, human, safe_addstr, truncate,
)
from .scanner import Scanner
from .trash import deletable, trash_paths

MODE_VOLUMES = "volumes"
MODE_SCAN = "scan"
MODE_BROWSE = "browse"

KEYS_ENTER = (curses.KEY_ENTER, 10, 13)
KEYS_BACK = (curses.KEY_BACKSPACE, 127, 8)


def list_volumes():
    vols = []
    seen = set()
    candidates = ["/"]
    if os.path.isdir("/Volumes"):
        try:
            candidates += sorted(
                os.path.join("/Volumes", n) for n in os.listdir("/Volumes"))
        except OSError:
            pass
    for p in candidates:
        rp = os.path.realpath(p)
        if rp in seen or not os.path.isdir(p):
            continue
        seen.add(rp)
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        if total <= 0:
            continue
        name = "Macintosh HD" if p == "/" else os.path.basename(p)
        vols.append({"path": p, "name": name, "total": total, "free": free})
    return vols


class App:
    def __init__(self, scr, start_path=None):
        self.scr = scr
        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(80)
        render.init_colors()

        self.mode = MODE_VOLUMES
        self.volumes = list_volumes()
        self.vol_rows = self._build_vol_rows()
        self.vol_idx = 0
        self.input_buf = None  # str while typing a custom path

        self.scanner = None
        self.free_bytes = 0
        self.cur = None
        self.sel = 0
        self.off = 0
        self.nav_stack = []  # (node, sel, off) breadcrumbs
        self.sort_by = "size"

        self.collector = []  # Nodes marked for deletion
        self.overlay = None  # None | "help" | "collector" | "confirm"
        self.col_sel = 0
        self.confirm = None  # (lines, on_yes)

        self.flash_msg = None
        self.flash_until = 0
        self.flash_attr = 0
        self.spin = 0

        if start_path:
            self.start_scan(start_path)

    # ------------------------------------------------------------- loop

    def run(self):
        while True:
            self.spin += 1
            self._tick()
            self._draw()
            ch = self.scr.getch()
            if ch == -1 or ch == curses.KEY_RESIZE:
                continue
            if not self._handle(ch):
                break
        if self.scanner:
            self.scanner.cancel = True

    def _tick(self):
        if self.mode == MODE_SCAN and self.scanner and self.scanner.done:
            self.mode = MODE_BROWSE
            self.cur = self.scanner.root
            self.sel = self.off = 0
            self.nav_stack = []
            s = self.scanner
            self.flash("Scanned %s items in %s (%d unreadable)"
                       % (f"{s.n_files + s.n_dirs:,}", fmt_time(s.elapsed()),
                          s.n_errors), PAIR_OK)

    def flash(self, msg, pair=PAIR_TEXT, secs=4.0):
        self.flash_msg = msg
        self.flash_attr = curses.color_pair(pair) | curses.A_BOLD
        self.flash_until = time.time() + secs

    # ------------------------------------------------------------ state

    def _build_vol_rows(self):
        rows = [("vol", v) for v in self.volumes]
        rows.append(("home", None))
        rows.append(("cwd", None))
        rows.append(("other", None))
        return rows

    def start_scan(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        if self.scanner:
            self.scanner.cancel = True
        self.scanner = Scanner(path)
        self.free_bytes = 0
        if os.path.ismount(path):
            try:
                st = os.statvfs(path)
                self.free_bytes = st.f_frsize * st.f_bavail
            except OSError:
                pass
        self.collector = []
        self.cur = None
        self.sel = self.off = 0
        self.nav_stack = []
        self.overlay = None
        self.scanner.start()
        self.mode = MODE_SCAN

    def view(self):
        """Children of the current node, in the current sort order."""
        if not self.cur or not self.cur.children:
            return []
        if self.sort_by == "name":
            return sorted(self.cur.children, key=lambda c: c.name.lower())
        return self.cur.children  # size-sorted after scan

    def at_scan_root(self):
        return self.scanner and self.cur is self.scanner.root

    # ------------------------------------------------------------- keys

    def _handle(self, ch):
        if self.overlay == "help":
            self.overlay = None
            return True
        if self.overlay == "confirm":
            return self._handle_confirm(ch)
        if self.overlay == "collector":
            return self._handle_collector(ch)
        if self.input_buf is not None:
            return self._handle_input(ch)
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("?"):
            self.overlay = "help"
            return True
        if self.mode == MODE_VOLUMES:
            return self._handle_volumes(ch)
        if self.mode == MODE_SCAN:
            return self._handle_scan(ch)
        return self._handle_browse(ch)

    def _handle_volumes(self, ch):
        n = len(self.vol_rows)
        if ch in (curses.KEY_UP, ord("k")):
            self.vol_idx = (self.vol_idx - 1) % n
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.vol_idx = (self.vol_idx + 1) % n
        elif ch in KEYS_ENTER:
            kind, v = self.vol_rows[self.vol_idx]
            if kind == "vol":
                self.start_scan(v["path"])
            elif kind == "home":
                self.start_scan("~")
            elif kind == "cwd":
                self.start_scan(os.getcwd())
            else:
                self.input_buf = ""
        elif ch == ord("r"):
            self.volumes = list_volumes()
            self.vol_rows = self._build_vol_rows()
        return True

    def _handle_scan(self, ch):
        if ch in (curses.KEY_LEFT, 27) or ch in KEYS_BACK:
            self.scanner.cancel = True
            self.mode = MODE_VOLUMES
        return True

    def _handle_browse(self, ch):
        v = self.view()
        if ch in (curses.KEY_UP, ord("k")):
            self.sel -= 1
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel += 1
        elif ch == curses.KEY_PPAGE:
            self.sel -= 10
        elif ch == curses.KEY_NPAGE:
            self.sel += 10
        elif ch in (curses.KEY_HOME, ord("g")):
            self.sel = 0
        elif ch in (curses.KEY_END, ord("G")):
            self.sel = len(v) - 1
        elif ch in KEYS_ENTER or ch in (curses.KEY_RIGHT, ord("l")):
            if v and 0 <= self.sel < len(v):
                node = v[self.sel]
                if node.is_dir:
                    self.nav_stack.append((self.cur, self.sel, self.off))
                    self.cur = node
                    self.sel = self.off = 0
                else:
                    self.flash("%s — %s (file)"
                               % (node.display_name(), human(node.size)))
        elif ch in (curses.KEY_LEFT, ord("h")) or ch in KEYS_BACK:
            if self.nav_stack:
                self.cur, self.sel, self.off = self.nav_stack.pop()
            else:
                self.mode = MODE_VOLUMES
        elif ch == ord("c"):
            self._toggle_collect(v)
        elif ch == ord("C"):
            self.overlay = "collector"
            self.col_sel = 0
        elif ch == ord("x"):
            self._confirm_empty()
        elif ch == ord("s"):
            self.sort_by = "name" if self.sort_by == "size" else "size"
            self.sel = self.off = 0
        elif ch == ord("r"):
            self.start_scan(self.scanner.root_path)
        elif ch == ord("o"):
            if v and 0 <= self.sel < len(v):
                try:
                    subprocess.Popen(
                        ["open", "-R", v[self.sel].path()],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    self.flash("Could not reveal in Finder", PAIR_WARN)
        self.sel = max(0, min(self.sel, len(v) - 1)) if v else 0
        return True

    def _handle_input(self, ch):
        if ch == 27:
            self.input_buf = None
        elif ch in KEYS_ENTER:
            path = os.path.expanduser(self.input_buf.strip())
            if os.path.isdir(path):
                self.input_buf = None
                self.start_scan(path)
            else:
                self.flash("Not a folder: %s" % (path or "(empty)"), PAIR_WARN)
        elif ch in KEYS_BACK:
            self.input_buf = self.input_buf[:-1]
        elif 32 <= ch < 127:
            self.input_buf += chr(ch)
        return True

    def _handle_confirm(self, ch):
        lines, on_yes = self.confirm
        if ch in (ord("y"), ord("Y")):
            self.overlay = None
            self.confirm = None
            on_yes()
        elif ch in (ord("n"), ord("N"), 27, ord("q")):
            self.overlay = None
            self.confirm = None
        return True

    def _handle_collector(self, ch):
        n = len(self.collector)
        if ch in (27, ord("C"), ord("q")):
            self.overlay = None
        elif ch in (curses.KEY_UP, ord("k")):
            self.col_sel = max(0, self.col_sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.col_sel = min(n - 1, self.col_sel + 1) if n else 0
        elif ch in (ord("d"), ord("c")) and n:
            self.collector.pop(self.col_sel)
            self.col_sel = max(0, min(self.col_sel, len(self.collector) - 1))
        elif ch == ord("x"):
            self._confirm_empty()
        return True

    # -------------------------------------------------------- collector

    def _toggle_collect(self, v):
        if not v or not (0 <= self.sel < len(v)):
            return
        node = v[self.sel]
        if node in self.collector:
            self.collector.remove(node)
            return
        if node is self.scanner.root or not deletable(node.path()):
            self.flash("Protected — refusing to collect %s"
                       % node.display_name(), PAIR_WARN)
            return
        self.collector.append(node)
        self.flash("Collected %s (%s) — %d item(s), %s total"
                   % (node.display_name(), human(node.size),
                      len(self.collector),
                      human(sum(x.size for x in self.collector))))

    def _dedup_collector(self):
        """Drop nodes whose ancestor is also collected."""
        keep = []
        cset = set(map(id, self.collector))
        for node in self.collector:
            p = node.parent
            shadowed = False
            while p is not None:
                if id(p) in cset:
                    shadowed = True
                    break
                p = p.parent
            if not shadowed:
                keep.append(node)
        return keep

    def _confirm_empty(self):
        items = self._dedup_collector()
        if not items:
            self.flash("Collector is empty — press c on an item first")
            return
        total = sum(n.size for n in items)
        lines = ["Move %d item(s) — %s — to the Trash?"
                 % (len(items), human(total)), ""]
        for n in items[:8]:
            lines.append("  " + truncate(n.path(), 58, head=True))
        if len(items) > 8:
            lines.append("  … and %d more" % (len(items) - 8))
        self.confirm = (lines, lambda: self._empty_collector(items))
        self.overlay = "confirm"

    def _empty_collector(self, items):
        pairs = [(n, n.path()) for n in items]
        ok, failed = trash_paths([p for _, p in pairs])
        okset = set(ok)
        freed = 0
        for node, path in pairs:
            if path in okset:
                freed += node.size
                self._detach(node)
                if node in self.collector:
                    self.collector.remove(node)
        self._fix_cur()
        if failed:
            self.flash("Trashed %s; %d item(s) failed (still collected)"
                       % (human(freed), len(failed)), PAIR_WARN, 6)
        else:
            self.collector = []
            self.flash("Moved %d item(s) to Trash — %s freed"
                       % (len(ok), human(freed)), PAIR_OK, 6)

    @staticmethod
    def _detach(node):
        p = node.parent
        if p and p.children and node in p.children:
            p.children.remove(node)
        while p is not None:
            p.size -= node.size
            p = p.parent

    def _fix_cur(self):
        """If the current folder was deleted, climb to a surviving ancestor."""
        while True:
            n = self.cur
            broken = None
            while n.parent is not None:
                if not n.parent.children or n not in n.parent.children:
                    broken = n.parent
                n = n.parent
            if broken is None:
                return
            self.cur = broken
            self.nav_stack = []
            self.sel = self.off = 0

    # ------------------------------------------------------------- draw

    def _draw(self):
        self.scr.erase()
        if self.mode == MODE_VOLUMES:
            self._draw_volumes()
        elif self.mode == MODE_SCAN:
            self._draw_scan()
        else:
            self._draw_browse()
        if self.overlay == "help":
            self._draw_help()
        elif self.overlay == "collector":
            self._draw_collector()
        elif self.overlay == "confirm":
            self._draw_confirm()

    def _header(self, text, right=""):
        _, w = self.scr.getmaxyx()
        safe_addstr(self.scr, 0, 0, " " * w, curses.A_REVERSE)
        safe_addstr(self.scr, 0, 1, truncate(text, w - len(right) - 4),
                    curses.A_REVERSE | curses.A_BOLD)
        if right:
            safe_addstr(self.scr, 0, w - len(right) - 1, right,
                        curses.A_REVERSE)

    def _footer(self, hints, right=""):
        h, w = self.scr.getmaxyx()
        if self.flash_msg and time.time() < self.flash_until:
            safe_addstr(self.scr, h - 1, 1, truncate(self.flash_msg, w - 2),
                        self.flash_attr)
            return
        safe_addstr(self.scr, h - 1, 1, truncate(hints, w - len(right) - 3),
                    curses.color_pair(PAIR_DIM))
        if right:
            attr = curses.color_pair(PAIR_WARN if self.collector else PAIR_DIM)
            safe_addstr(self.scr, h - 1, w - len(right) - 1, right, attr)

    # volumes screen -----------------------------------------------------

    def _draw_volumes(self):
        h, w = self.scr.getmaxyx()
        self._header("floppydisk", "v0.1")
        y = 2
        for i, line in enumerate(render.LOGO):
            safe_addstr(self.scr, y + i, 3, line,
                        curses.color_pair(render.hue_pair_no(i * 4, 1))
                        | curses.A_BOLD)
        safe_addstr(self.scr, y + len(render.LOGO), 3,
                    "a DaisyDisk-style disk map for your terminal",
                    curses.color_pair(PAIR_DIM))
        # floppy art on the right
        art_x = w - 22
        if art_x > 46:
            led_on = (self.spin // 6) % 2 == 0
            for i, line in enumerate(render.FLOPPY):
                line = line if led_on else line.replace(render.FLOPPY_LED, "░░")
                safe_addstr(self.scr, y + i, art_x, line,
                            curses.color_pair(PAIR_HEAD))
        y += len(render.LOGO) + 2
        safe_addstr(self.scr, y, 3, "Select a volume or folder to scan:",
                    curses.color_pair(PAIR_TEXT) | curses.A_BOLD)
        y += 2
        bar_w = 22
        for i, (kind, v) in enumerate(self.vol_rows):
            if y + i >= h - 2:
                break
            sel = i == self.vol_idx
            attr = curses.A_REVERSE if sel else 0
            marker = "▸ " if sel else "  "
            if kind == "vol":
                used = v["total"] - v["free"]
                frac = used / v["total"]
                pct = int(round(frac * 100))
                bpair = PAIR_WARN if frac > 0.9 else PAIR_BAR
                label = truncate(v["name"], 18).ljust(18)
                safe_addstr(self.scr, y + i, 3, marker + "◆ " + label, attr)
                safe_addstr(self.scr, y + i, 25, "[", curses.color_pair(PAIR_DIM))
                safe_addstr(self.scr, y + i, 26, bar_str(frac, bar_w),
                            curses.color_pair(bpair))
                safe_addstr(self.scr, y + i, 26 + bar_w, "]",
                            curses.color_pair(PAIR_DIM))
                info = "%s free of %s (%d%% used)" % (
                    human(v["free"]), human(v["total"]), pct)
                safe_addstr(self.scr, y + i, 29 + bar_w, info, attr)
            elif kind == "home":
                safe_addstr(self.scr, y + i, 3,
                            marker + "⌂ Home folder        ~", attr)
            elif kind == "cwd":
                safe_addstr(self.scr, y + i, 3, marker + "▸ Current folder     "
                            + truncate(os.getcwd(), w - 30, head=True), attr)
            else:
                safe_addstr(self.scr, y + i, 3,
                            marker + "… Other folder       (type a path)", attr)
        if self.input_buf is not None:
            safe_addstr(self.scr, h - 2, 3, "Scan path: %s▁" % self.input_buf,
                        curses.color_pair(PAIR_HEAD) | curses.A_BOLD)
            self._footer("⏎ scan   esc cancel")
        else:
            self._footer("↑↓ select   ⏎ scan   r refresh   ? help   q quit")

    # scan screen --------------------------------------------------------

    def _draw_scan(self):
        h, w = self.scr.getmaxyx()
        s = self.scanner
        self._header("floppydisk — scanning " + s.root_path)
        sun_w = max(20, w // 2)
        if s.root.size > 0 and w >= 60:
            rings, starts, _ = render.build_segments(
                s.root, 4, self.free_bytes)
            center = [
                (s.root.display_name(), curses.color_pair(PAIR_TEXT) | curses.A_BOLD),
                (human(s.root.size), curses.color_pair(PAIR_HEAD)),
            ]
            render.draw_sunburst(self.scr, 1, 0, h - 2, sun_w,
                                 rings, starts, center)
        x = sun_w + 3
        y = max(2, h // 2 - 8)
        led_on = (self.spin // 3) % 2 == 0
        for i, line in enumerate(render.FLOPPY):
            line = line if led_on else line.replace(render.FLOPPY_LED, "░░")
            safe_addstr(self.scr, y + i, x, line, curses.color_pair(PAIR_HEAD))
        y += len(render.FLOPPY) + 1
        spin = render.SPINNER[self.spin % len(render.SPINNER)]
        stats = [
            ("%s scanning…" % spin, curses.A_BOLD),
            ("", 0),
            ("items    %s" % f"{s.n_files + s.n_dirs:,}", 0),
            ("size     %s" % human(s.root.size), 0),
            ("errors   %d" % s.n_errors, 0),
            ("elapsed  %s" % fmt_time(s.elapsed()), 0),
            ("", 0),
            (truncate(s.current, w - x - 2, head=True),
             curses.color_pair(PAIR_DIM)),
        ]
        for i, (line, attr) in enumerate(stats):
            safe_addstr(self.scr, y + i, x, line, attr)
        self._footer("← cancel   q quit")

    # browse screen ------------------------------------------------------

    def _draw_browse(self):
        h, w = self.scr.getmaxyx()
        s = self.scanner
        path = self.cur.path()
        right = ""
        if self.at_scan_root() and self.free_bytes:
            right = "%s used • %s free" % (human(self.cur.size),
                                           human(self.free_bytes))
        else:
            right = human(self.cur.size)
        self._header("floppydisk — " + path, right)

        body_h = h - 2
        show_sun = w >= 76
        sun_w = w // 2 if show_sun else 0
        list_x = sun_w + 2 if show_sun else 1
        list_w = w - list_x - 1

        v = self.view()
        selected = v[self.sel] if v and 0 <= self.sel < len(v) else None
        free = self.free_bytes if self.at_scan_root() else 0
        depth = 4 if (not show_sun or min(sun_w // 2, body_h) > 18) else 3
        rings, starts, cmap = render.build_segments(
            self.cur, depth, free, selected)

        if show_sun:
            center = [(self.cur.display_name() or path,
                       curses.color_pair(PAIR_TEXT) | curses.A_BOLD),
                      (human(self.cur.size), curses.color_pair(PAIR_HEAD)),
                      ("%d items" % len(v), curses.color_pair(PAIR_DIM))]
            if free:
                center.append(("%s free" % human(free),
                               curses.color_pair(PAIR_FREE)))
            render.draw_sunburst(self.scr, 1, 0, body_h, sun_w,
                                 rings, starts, center)
            for yy in range(1, h - 1):
                safe_addstr(self.scr, yy, sun_w, "│",
                            curses.color_pair(PAIR_DIM))

        # file list
        rows = body_h - 1
        if self.sel < self.off:
            self.off = self.sel
        if self.sel >= self.off + rows:
            self.off = self.sel - rows + 1
        total = self.cur.size or 1
        maxsz = max((c.size for c in v), default=1) or 1
        bar_w = 10
        name_w = max(8, list_w - bar_w - 22)
        hdr = "  %s %s %9s %5s" % ("name".ljust(name_w), " " * bar_w,
                                   "size", "%")
        safe_addstr(self.scr, 1, list_x, truncate(hdr, list_w),
                    curses.color_pair(PAIR_DIM) | curses.A_UNDERLINE)
        for i in range(rows - 1):
            idx = self.off + i
            if idx >= len(v):
                break
            node = v[idx]
            y = 2 + i
            is_sel = idx == self.sel
            row_attr = curses.A_REVERSE if is_sel else 0
            pair = cmap.get(node, PAIR_DIM)
            collected = node in self.collector
            icon = "▸" if node.is_dir else "·"
            mark = "✗" if collected else " "
            name = truncate(node.display_name(), name_w - 2)
            frac = node.size / total
            line = "%s %s %s" % (mark, icon, name)
            safe_addstr(self.scr, y, list_x, " " * min(list_w, w - list_x),
                        row_attr)
            safe_addstr(self.scr, y, list_x, "●",
                        curses.color_pair(pair) | row_attr)
            attr = row_attr | (curses.color_pair(PAIR_WARN) if collected
                               else curses.color_pair(PAIR_TEXT))
            safe_addstr(self.scr, y, list_x + 2, line.ljust(name_w + 2), attr)
            bx = list_x + name_w + 5
            safe_addstr(self.scr, y, bx, bar_str(node.size / maxsz, bar_w),
                        curses.color_pair(pair) | row_attr)
            safe_addstr(self.scr, y, bx + bar_w + 1,
                        "%9s %4d%%" % (human(node.size), round(frac * 100)),
                        row_attr)
        more = len(v) - (self.off + rows - 1)
        if more > 0:
            safe_addstr(self.scr, h - 2, list_x, "… %d more" % more,
                        curses.color_pair(PAIR_DIM))
        csize = sum(n.size for n in self.collector)
        cinfo = ("✗ %d collected • %s" % (len(self.collector), human(csize))
                 if self.collector else "")
        self._footer("↑↓ move  ⏎ open  ⌫ up  c collect  C bin  x trash  "
                     "s sort  r rescan  o reveal  ? help  q quit", cinfo)

    # overlays -----------------------------------------------------------

    def _overlay_box(self, bh, bw, title):
        h, w = self.scr.getmaxyx()
        bw = min(bw, w - 2)
        bh = min(bh, h - 2)
        y = (h - bh) // 2
        x = (w - bw) // 2
        draw_box(self.scr, y, x, bh, bw, title)
        return y, x, bh, bw

    def _draw_help(self):
        lines = [
            "↑/↓, j/k       select item",
            "⏎, →, l        open folder",
            "⌫, ←, h        go up (volume list from the top)",
            "g / G          jump to first / last",
            "c              collect item for deletion (again to uncollect)",
            "C              open the collector bin",
            "x              empty collector → move to Trash",
            "s              sort by size / name",
            "r              rescan",
            "o              reveal selection in Finder",
            "q              quit",
            "",
            "The sunburst map shows the current folder: inner ring is its",
            "direct children, outer rings their contents. The textured",
            "slice is the current selection; grey is free space and",
            "pooled small files.",
        ]
        y, x, bh, bw = self._overlay_box(len(lines) + 4, 68, "help")
        for i, line in enumerate(lines[: bh - 4]):
            safe_addstr(self.scr, y + 2 + i, x + 3, truncate(line, bw - 6))

    def _draw_collector(self):
        items = self.collector
        total = sum(n.size for n in items)
        title = "collector — %d item(s), %s" % (len(items), human(total))
        y, x, bh, bw = self._overlay_box(max(9, min(len(items), 14) + 7),
                                         74, title)
        if not items:
            safe_addstr(self.scr, y + 2, x + 3,
                        "Empty. Press c on files or folders to collect them.",
                        curses.color_pair(PAIR_DIM))
        self.col_sel = max(0, min(self.col_sel, len(items) - 1)) if items else 0
        vis = bh - 5
        off = max(0, min(self.col_sel - vis + 1, len(items) - vis))
        for i in range(min(vis, len(items))):
            node = items[off + i]
            attr = curses.A_REVERSE if off + i == self.col_sel else 0
            safe_addstr(self.scr, y + 2 + i, x + 3,
                        truncate(node.path(), bw - 18, head=True).ljust(bw - 18)
                        + " %9s" % human(node.size), attr)
        safe_addstr(self.scr, y + bh - 2, x + 3,
                    "d remove from bin   x empty to Trash   esc close",
                    curses.color_pair(PAIR_DIM))

    def _draw_confirm(self):
        lines, _ = self.confirm
        bw = max(len(x) for x in lines) + 8
        y, x, bh, bw = self._overlay_box(len(lines) + 5, max(bw, 44),
                                         "confirm")
        for i, line in enumerate(lines[: bh - 5]):
            safe_addstr(self.scr, y + 2 + i, x + 3, truncate(line, bw - 6))
        safe_addstr(self.scr, y + bh - 2, x + 3, "y",
                    curses.color_pair(PAIR_OK) | curses.A_BOLD)
        safe_addstr(self.scr, y + bh - 2, x + 4, "es, trash them   ")
        safe_addstr(self.scr, y + bh - 2, x + 21, "n",
                    curses.color_pair(PAIR_WARN) | curses.A_BOLD)
        safe_addstr(self.scr, y + bh - 2, x + 22, "o, keep everything")

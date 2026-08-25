"""Filesystem scanning: builds a size tree on a background thread."""

import os
import threading
import time

BLOCK = 512

# Never descend into these exact paths. On macOS, scanning "/" must not
# recurse into other mounted volumes, devfs, or the APFS volume group
# mountpoints (their content is already reachable through firmlinks like
# /Users and /Applications — descending would double count it).
SKIP_DIRS = {
    "/Volumes",
    "/dev",
    "/System/Volumes",
    "/Network",
}


class Node:
    __slots__ = ("name", "size", "parent", "children", "is_dir")

    def __init__(self, name, parent=None, is_dir=False):
        self.name = name
        self.size = 0
        self.parent = parent
        self.children = [] if is_dir else None
        self.is_dir = is_dir

    def path(self):
        parts = []
        n = self
        while n is not None:
            parts.append(n.name)
            n = n.parent
        parts.reverse()
        if len(parts) == 1:
            return parts[0]
        return os.path.join(parts[0], *parts[1:])

    def display_name(self):
        return os.path.basename(self.name.rstrip("/")) or self.name


class Scanner(threading.Thread):
    """Walks a directory tree, accumulating on-disk sizes up to the root."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.root_path = os.path.abspath(path)
        self.root = Node(self.root_path, None, True)
        self.n_files = 0
        self.n_dirs = 0
        self.n_errors = 0
        self.current = ""
        self.done = False
        self.cancel = False
        self.started_at = time.time()
        self.finished_at = None
        self._seen = set()  # (dev, inode) of hardlinked files already counted

    def run(self):
        try:
            self._walk()
        except Exception:
            self.n_errors += 1
        self._sort()
        self.finished_at = time.time()
        self.done = True

    def elapsed(self):
        return (self.finished_at or time.time()) - self.started_at

    def _walk(self):
        stack = [(self.root, self.root_path)]
        while stack and not self.cancel:
            node, path = stack.pop()
            self.current = path
            try:
                it = os.scandir(path)
            except OSError:
                self.n_errors += 1
                continue
            with it:
                for entry in it:
                    if self.cancel:
                        return
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        self.n_errors += 1
                        continue
                    blocks = getattr(st, "st_blocks", None)
                    size = blocks * BLOCK if blocks is not None else st.st_size
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if not is_dir and getattr(st, "st_nlink", 1) > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in self._seen:
                            continue
                        self._seen.add(key)
                    child = Node(entry.name, node, is_dir)
                    node.children.append(child)
                    p = child
                    while p is not None:
                        p.size += size
                        p = p.parent
                    if is_dir:
                        self.n_dirs += 1
                        full = os.path.join(path, entry.name)
                        if full not in SKIP_DIRS:
                            stack.append((child, full))
                    else:
                        self.n_files += 1

    def _sort(self):
        stack = [self.root]
        while stack:
            n = stack.pop()
            if n.children:
                n.children.sort(key=lambda c: c.size, reverse=True)
                stack.extend(c for c in n.children if c.is_dir)

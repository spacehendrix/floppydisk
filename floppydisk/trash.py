"""Moving files to the Trash, with safety guards."""

import os
import shutil
import subprocess

PROTECTED = {
    "/", "/System", "/Library", "/Users", "/Applications", "/usr", "/bin",
    "/sbin", "/etc", "/var", "/private", "/opt", "/Volumes", "/dev", "/home",
    os.path.expanduser("~"),
}


def deletable(path):
    """Refuse system roots, mount points, and anything inside /System."""
    rp = os.path.abspath(path)
    if rp in PROTECTED or rp.startswith("/System/"):
        return False
    try:
        if os.path.ismount(rp):
            return False
    except OSError:
        pass
    return True


def _applescript_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _finder_trash(paths):
    items = ", ".join('POSIX file "%s"' % _applescript_escape(p) for p in paths)
    script = 'tell application "Finder" to delete {%s}' % items
    try:
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _trash_dir_for(path):
    if path.startswith("/Volumes/"):
        vol = "/".join(path.split("/")[:3])
        cand = os.path.join(vol, ".Trashes", str(os.getuid()))
        try:
            os.makedirs(cand, exist_ok=True)
            if os.access(cand, os.W_OK):
                return cand
        except OSError:
            pass
        return None
    return os.path.expanduser("~/.Trash")


def _manual_trash(path):
    dest_dir = _trash_dir_for(path)
    if not dest_dir or not os.path.isdir(dest_dir):
        return False
    base = os.path.basename(path.rstrip("/")) or "item"
    dest = os.path.join(dest_dir, base)
    n = 1
    while os.path.lexists(dest):
        root, ext = os.path.splitext(base)
        dest = os.path.join(dest_dir, "%s %d%s" % (root, n, ext))
        n += 1
    try:
        os.rename(path, dest)
        return True
    except OSError:
        try:
            shutil.move(path, dest)
            return True
        except (OSError, shutil.Error):
            return False


def trash_paths(paths):
    """Move each path to the Trash. Returns (ok_paths, failed_paths)."""
    paths = [p for p in paths if os.path.lexists(p)]
    if not paths:
        return [], []
    if shutil.which("osascript"):
        _finder_trash(paths)
    ok, failed = [], []
    for p in paths:
        if not os.path.lexists(p):
            ok.append(p)
        elif _manual_trash(p):
            ok.append(p)
        else:
            failed.append(p)
    return ok, failed

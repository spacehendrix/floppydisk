import argparse
import curses
import locale
import os
import sys

from . import __version__
from .app import App


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="floppydisk",
        description="A DaisyDisk-style disk usage scanner for the terminal.")
    ap.add_argument("path", nargs="?",
                    help="folder or volume to scan (skips the volume picker)")
    ap.add_argument("--version", action="version",
                    version="floppydisk %s" % __version__)
    args = ap.parse_args(argv)

    if args.path:
        p = os.path.abspath(os.path.expanduser(args.path))
        if not os.path.isdir(p):
            print("floppydisk: not a folder: %s" % args.path, file=sys.stderr)
            return 2
        args.path = p

    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda scr: App(scr, args.path).run())
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        print("floppydisk: terminal error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

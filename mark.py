"""MARK LIV command-line entry point."""
from __future__ import annotations

import sys


def main() -> int:
    command = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if command == "doctor":
        from doctor import Doctor
        return Doctor().run()
    print("Usage: python mark.py doctor")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

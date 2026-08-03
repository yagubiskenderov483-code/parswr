#!/usr/bin/env python3
"""Разово стереть старую БД и мусор от старого бота."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def main() -> None:
    DATA.mkdir(exist_ok=True)
    removed: list[str] = []

    for path in [
        *DATA.glob("*.db"),
        *DATA.glob("*.db-*"),
        ROOT / "bot.db",
    ]:
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(str(path))

    old_pkg = ROOT / "bot"
    if old_pkg.is_dir():
        shutil.rmtree(old_pkg, ignore_errors=True)
        removed.append(str(old_pkg))

    print("Wiped:" if removed else "Nothing to wipe.")
    for item in removed:
        print(" -", item)


if __name__ == "__main__":
    main()

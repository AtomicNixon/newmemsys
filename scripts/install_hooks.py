"""Install NewMemSys compact-interceptor hooks into Claude Code settings.json.

Idempotent: safe to re-run. Only touches the `hooks` section of
~/.claude/settings.json (or the Windows equivalent). Backs up the original
file to settings.json.bak before modifying.

Usage:
    python scripts/install_hooks.py

To uninstall:
    python scripts/install_hooks.py --uninstall
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


SETTINGS_NAME = "settings.json"


def _settings_path() -> Path:
    """Return Claude Code's user settings.json path."""
    home = Path.home()
    return home / ".claude" / SETTINGS_NAME


def _hook_path(name: str) -> str:
    """Return absolute path to a hook script, using forward slashes."""
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "scripts" / "hooks" / name
    # Use the same Python interpreter that runs this installer.
    python = sys.executable.replace("\\", "/")
    return f'"{python}" "{str(path).replace(chr(92), "/")}"'


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(".json.bak")
    if path.exists():
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def install():
    path = _settings_path()
    settings = _load_settings(path)
    hooks = settings.setdefault("hooks", {})

    hooks["PreCompact"] = _hook_path("precompact.py")
    hooks["PostCompact"] = _hook_path("postcompact.py")
    hooks.setdefault("SessionEnd", _hook_path("session_end.py"))

    _save_settings(path, settings)
    print(f"Installed NewMemSys hooks into {path}")
    print(f"  PreCompact:  {hooks['PreCompact']}")
    print(f"  PostCompact: {hooks['PostCompact']}")
    print(f"  SessionEnd:  {hooks['SessionEnd']}")
    print("Backup written to settings.json.bak")


def uninstall():
    path = _settings_path()
    settings = _load_settings(path)
    hooks = settings.get("hooks", {})

    for key in ("PreCompact", "PostCompact", "SessionEnd"):
        if key in hooks:
            del hooks[key]

    _save_settings(path, settings)
    print(f"Removed NewMemSys hooks from {path}")


def main():
    parser = argparse.ArgumentParser(description="Install/uninstall NewMemSys Claude Code hooks")
    parser.add_argument("--uninstall", action="store_true", help="Remove the hooks")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()

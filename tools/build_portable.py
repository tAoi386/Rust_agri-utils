"""Promote a staged portable build while preserving projects and stock data."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def within_workspace(path, root):
    resolved = Path(path).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"Build path must stay inside the project: {resolved}")
    return resolved


def assert_not_running(executable):
    if sys.platform != "win32":
        return
    query = ("$ErrorActionPreference='Stop'; $p=@(Get-Process -Name RustSeedLab -ErrorAction SilentlyContinue); "
             "@($p | ForEach-Object { if(-not $_.Path){throw 'Cannot inspect the running program.'}; "
             "$_.Path }) | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
                            capture_output=True, text=True, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    paths = json.loads(result.stdout.strip() or "[]")
    paths = [paths] if isinstance(paths, str) else paths
    if any(path and Path(path).resolve() == executable for path in (paths or [])):
        raise RuntimeError("Close the current dist/capture/RustSeedLab program, then run build.bat again.")


def promote_portable(stage, destination, *, root=ROOT, migration=None):
    root = Path(root).resolve()
    stage = within_workspace(stage, root)
    destination = within_workspace(destination, root)
    if stage == destination or stage.is_relative_to(destination) or destination.is_relative_to(stage):
        raise ValueError("Build staging and installed portable folders must be separate.")
    if not (stage / "RustSeedLab.exe").is_file() or not (stage / "_internal").is_dir():
        raise ValueError("The staged portable build is incomplete. Run PyInstaller first.")
    previous = destination if destination.exists() else None
    if previous is not None:
        assert_not_running(destination / "RustSeedLab.exe")
    elif migration is not None:
        previous = within_workspace(migration, root)
    if previous is not None and previous.is_dir():
        projects = list(previous.glob("*.json"))
        if (previous / "data").is_dir():
            projects.append(previous / "data")
        for source in projects:
            within_workspace(source, root)
            if source.is_dir():
                shutil.copytree(source, stage / source.name, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(source, stage / source.name)
    shutil.copy2(root / "README.md", stage / "README.md")
    for name in ('README.en.md', 'README.ru.md', 'LICENSE.md'):
        if (root / name).is_file():
            shutil.copy2(root / name, stage / name)
    if (root / 'resources/screenshots').is_dir():
        shutil.copytree(root / 'resources/screenshots', stage / 'resources/screenshots', dirs_exist_ok=True)
    shutil.copytree(root / "examples", stage / "examples", dirs_exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if destination.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = within_workspace(root / "artifacts" / "portable-build-backups" / stamp / "RustSeedLab", root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        destination.rename(backup)
    try:
        stage.rename(destination)
    except OSError:
        if backup is not None and not destination.exists():
            backup.rename(destination)
        raise
    return backup


if __name__ == "__main__":
    try:
        backup = promote_portable(ROOT / "dist" / "build-new" / "RustSeedLab",
                                  ROOT / "dist" / "capture" / "RustSeedLab",
                                  migration=ROOT / "dist" / "beginner" / "RustSeedLab")
        print("Portable build ready: dist/capture/RustSeedLab/RustSeedLab.exe")
        if backup is not None:
            print(f"Previous portable folder retained for recovery: {backup}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Portable update stopped safely; staged build retained. {exc}")

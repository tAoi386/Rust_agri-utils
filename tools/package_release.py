"""Collect the built portable folder and editable project into release archives."""
from pathlib import Path
import json
import runpy
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "dist" / "capture" / "RustSeedLab"
ARTIFACTS = ROOT / "artifacts"


def package_release():
    if not (PACKAGE / "RustSeedLab.exe").exists():
        raise SystemExit("Run build.bat first.")
    expected_version = runpy.run_path(str(ROOT / "rust_breeder" / "__init__.py"))["__version__"]
    try:
        checked = json.loads((PACKAGE / "artifacts" / "packaged-check.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise SystemExit("Run the built RustSeedLab.exe --smoke-test first.")
    if not isinstance(checked, dict) or checked.get("ok") is not True or checked.get("version") != expected_version:
        raise SystemExit(f"Rebuild and run RustSeedLab.exe --smoke-test for version {expected_version} first.")
    ARTIFACTS.mkdir(exist_ok=True)
    for name in ('README.md', 'README.en.md', 'README.ru.md', 'LICENSE.md'):
        shutil.copy2(ROOT / name, PACKAGE / name)
    shutil.copytree(ROOT / "examples", PACKAGE / "examples", dirs_exist_ok=True)
    shutil.copytree(ROOT / "resources" / "screenshots", PACKAGE / "resources" / "screenshots", dirs_exist_ok=True)
    notices = PACKAGE / "third-party-notices"
    notices.mkdir(exist_ok=True)
    for name in ("pyside6_essentials", "shiboken6"):
        for info in (ROOT / ".deps").glob(f"{name}-*.dist-info"):
            target = notices / info.name
            target.mkdir(exist_ok=True)
            if (info / "METADATA").exists():
                shutil.copy2(info / "METADATA", target / "METADATA")
            if (info / "licenses").exists():
                shutil.copytree(info / "licenses", target / "licenses", dirs_exist_ok=True)
    python_license = Path(__import__("sys").base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, notices / "Python-LICENSE.txt")
    with zipfile.ZipFile(ARTIFACTS / "RustSeedLab-Windows.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source in sorted(PACKAGE.rglob("*")):
            relative = source.relative_to(PACKAGE)
            packaged = (relative.parts[0] in ("_internal", "third-party-notices")
                        or relative.as_posix() in ("RustSeedLab.exe", "README.md", "README.en.md", "README.ru.md", "LICENSE.md")
                        or relative.as_posix() in ("examples/beginner.json", "examples/parallel.json")
                        or (relative.parts[:2] == ('resources', 'screenshots') and source.name in {
                            f'{mode}-{language}.png' for mode in ('sequential', 'parallel')
                            for language in ('zh_CN', 'en', 'ru')})
                        or (relative.parts[0] == "artifacts" and (source.name == 'packaged-check.json'
                            or source.name == 'packaged-history.png'
                            or source.name in {f'packaged-{mode}-{language}.png'
                                               for mode in ('sequential', 'parallel')
                                               for language in ('zh_CN', 'en', 'ru')})))
            if source.is_file() and packaged:
                archive.write(source, source.relative_to(PACKAGE.parent))
    includes = ["rust_breeder", "tests", "tools", "resources", "examples"]
    source_files = [ROOT / name for name in ("main.py", "README.md", "README.en.md", "README.ru.md", "LICENSE.md",
                    "requirements.txt", "launch.bat", "build.bat", "RustSeedLab.spec",
                    "启动种养殖工具.bat", "启动苗圃.bat", ".gitignore")]
    for directory in includes:
        source_files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    with zipfile.ZipFile(ARTIFACTS / "RustSeedLab-Source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(source_files):
            archive.write(source, Path("RustSeedLab-Source") / source.relative_to(ROOT))
    for name in ("RustSeedLab-Windows.zip", "RustSeedLab-Source.zip"):
        output = ARTIFACTS / name
        print(f"{output.name}: {output.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    package_release()

#!/usr/bin/env python3
"""Build a distributable skill from an explicit source-file allowlist."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import zipfile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    files = [root / name for name in ("SKILL.md", "README.md", "requirements.txt", "agents/openai.yaml")]
    files += sorted((root / "scripts").glob("*.py"))
    files += sorted((root / "references").glob("*.md"))
    engine = root / "vendor/instsci"
    files += [engine / name for name in ("LICENSE", "pyproject.toml", "UPSTREAM.md")]
    for directory, dirs, names in os.walk(engine / "instsci", followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and d not in {"_browsers", "__pycache__"}
                         and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix == ".py" or path.parent == engine / "instsci/data" and path.suffix in {".json", ".md"}:
                files.append(path)
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing source file or unexpected symlink: {path.relative_to(root)}")
    output = root / "dist/download-papers.skill"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".skill.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(files):
                archive.write(path, Path("download-papers") / path.relative_to(root))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("Archive integrity check failed")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".skill.sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(f"Built {output.name}: {len(files)} files, {output.stat().st_size} bytes")


if __name__ == "__main__":
    main()

from __future__ import annotations

import os
from pathlib import Path


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    if value and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()

    return key, value


def _candidate_env_files(filename: str = ".env") -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    for start in (Path.cwd(), Path(__file__).resolve().parents[1]):
        current = start.resolve()
        for directory in (current, *current.parents):
            candidate = directory / filename
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)

    return candidates


def load_env_file(env_file: str | Path = ".env", *, override: bool = False) -> Path | None:
    filename = Path(env_file)
    if filename.is_absolute():
        candidates = [filename]
    else:
        candidates = _candidate_env_files(filename.name)

    for candidate in candidates:
        if not candidate.is_file():
            continue

        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(raw_line)
            if not parsed:
                continue
            key, value = parsed
            if override or key not in os.environ:
                os.environ[key] = value
        return candidate

    return None

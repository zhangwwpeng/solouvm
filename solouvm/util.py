"""Small stdlib-only helpers shared by every stage."""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ConfigError, InfraError

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_DURATION_FACTOR = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value: str | float) -> float:
    """Parse ``500ms`` / ``60s`` / ``1h30m`` into seconds.

    A bare number is interpreted as seconds.
    """
    if isinstance(value, bool):
        raise ConfigError(f"invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ConfigError(f"duration must be > 0, got {value!r}")
        return float(value)

    text = str(value).strip().replace(" ", "")
    if not text:
        raise ConfigError("duration must not be empty")

    total = 0.0
    pos = 0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:
            raise ConfigError(f"invalid duration: {value!r}")
        total += float(match.group(1)) * _DURATION_FACTOR[match.group(2)]
        pos = match.end()
    if pos != len(text) or total <= 0:
        raise ConfigError(
            f"invalid duration: {value!r}",
            hint="use forms like 500ms, 60s, 20m, 1h30m",
        )
    return total


def parse_seeds(spec: int | str | list[int] | tuple[int, ...]) -> list[int]:
    """Normalise the ``regress.seeds`` setting into an ordered, de-duplicated list."""
    if isinstance(spec, bool):
        raise ConfigError(f"invalid seeds: {spec!r}")
    if isinstance(spec, int):
        if spec < 0:
            raise ConfigError(f"seeds count must be >= 0, got {spec}")
        if spec == 0:
            return []
        rng = random.SystemRandom()
        chosen: list[int] = []
        seen: set[int] = set()
        while len(chosen) < spec:
            seed = rng.randint(1, 999_999)
            if seed not in seen:
                seen.add(seed)
                chosen.append(seed)
        return chosen
    if isinstance(spec, str):
        return _parse_seed_string(spec)
    if isinstance(spec, (list, tuple)):
        out: list[int] = []
        for item in spec:
            if isinstance(item, str):
                out.extend(_parse_seed_string(item))
            elif isinstance(item, int) and not isinstance(item, bool):
                out.append(item)
            else:
                raise ConfigError(f"invalid seed: {item!r}")
        return _dedupe(out)
    raise ConfigError(f"invalid seeds: {spec!r}")


def _parse_seed_string(text: str) -> list[int]:
    out: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk.lstrip("-"):
            lo, _, hi = chunk.partition("-")
            try:
                start, stop = int(lo), int(hi)
            except ValueError:
                raise ConfigError(f"invalid seed range: {chunk!r}") from None
            if stop < start:
                raise ConfigError(f"invalid seed range: {chunk!r}")
            out.extend(range(start, stop + 1))
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                raise ConfigError(f"invalid seed: {chunk!r}") from None
    return _dedupe(out)


def _dedupe(values: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def slug(text: str) -> str:
    """Turn an arbitrary case name into a filesystem-safe token."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return cleaned or "case"


def case_id(name: str, seed: int) -> str:
    return f"{slug(name)}__{seed}"


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{os.urandom(3).hex()}"


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise InfraError(f"missing file: {path}") from None
    except json.JSONDecodeError as exc:
        raise InfraError(f"malformed JSON in {path}: {exc}") from None


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def relpath(path: Path, root: Path) -> str:
    """Best-effort project-relative path; absolute when ``path`` escapes ``root``."""
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())

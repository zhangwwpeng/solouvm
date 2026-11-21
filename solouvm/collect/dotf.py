"""Parse ``.f`` files: nested ``-f`` / ``-F``, ``+incdir+``, ``+define+``, libs (config.md §4.2)."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from solouvm.errors import ConfigError

MAX_DEPTH = 32  # 嵌套 .f 的深度上限（防环）

# 带一个参数的选项
_FILE_OPTS = ("-f", "-F")
_LIB_OPTS = ("-v", "-y")
_PLUS_OPTS = ("+incdir+", "+define+", "+libext+")

# 行内注释：行首或空白后的 //
_COMMENT_RE = re.compile(r"(^|\s)//.*$")


def parse_dotf(path: Path, *, relative_to_dotf: bool) -> list[str]:
    """Return the flattened tokens of ``path`` with nested ``.f`` files expanded.

    File paths, ``+incdir+`` directories and ``-v`` / ``-y`` arguments are joined with the
    base directory: the ``.f``'s own directory for ``-F`` (``relative_to_dotf=True``), the
    current working directory for ``-f``. ``$VAR`` paths and absolute paths are kept as is.
    ``-v`` / ``-y`` come out as two tokens (option, path).
    """
    items = walk_dotf(Path(path), relative_to_dotf=relative_to_dotf)
    return [token for token, _source, _joined in items]


def walk_dotf(
    path: Path,
    *,
    relative_to_dotf: bool,
    _depth: int = 1,
    _stack: tuple[Path, ...] = (),
) -> list[tuple[str, Path, bool]]:
    """Like :func:`parse_dotf`, as ``(token, .f that listed it, joined with a base dir)``."""
    if _depth > MAX_DEPTH:
        raise ConfigError(
            f".f files nested deeper than {MAX_DEPTH} levels at {path}", code="CFG_INVALID"
        )
    if not path.is_file():
        raise ConfigError(f".f file not found: {path}", code="FILES_MISSING")
    key = path.resolve()
    if key in _stack:
        chain = " -> ".join(str(p) for p in (*_stack, key))
        raise ConfigError(f"cyclic .f include: {chain}", code="CFG_INVALID")
    stack = (*_stack, key)

    base = path.parent if relative_to_dotf else Path.cwd()
    out: list[tuple[str, Path, bool]] = []
    tokens = _tokens(path)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _FILE_OPTS or token in _LIB_OPTS:
            if i + 1 >= len(tokens):
                raise ConfigError(f"{path}: option {token} needs an argument", code="CFG_INVALID")
            arg = join_base(base, tokens[i + 1])
            i += 2
            if token in _LIB_OPTS:
                out += [(token, path, False), (arg, path, False)]
                continue
            out += walk_dotf(
                Path(arg),
                relative_to_dotf=token == "-F",
                _depth=_depth + 1,
                _stack=stack,
            )
            continue
        i += 1
        if token.startswith("+incdir+"):
            dirs = [d for d in token.removeprefix("+incdir+").split("+") if d]
            out += [("+incdir+" + join_base(base, d), path, False) for d in dirs]
        elif token.startswith(_PLUS_OPTS):
            out.append((token, path, False))
        elif token.startswith(("-", "+")):
            raise ConfigError(f"{path}: unsupported .f option {token!r}", code="CFG_INVALID")
        else:
            joined = join_base(base, token)
            out.append((joined, path, joined != token))
    return out


def join_base(base: Path, token: str) -> str:
    """Join a relative path with ``base``; ``$VAR`` and absolute paths stay untouched."""
    if "$" in token or os.path.isabs(token):
        return token
    return os.path.normpath(os.path.join(str(base), token))


def _tokens(path: Path) -> list[str]:
    tokens: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = _COMMENT_RE.sub("", raw).strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens += shlex.split(line)
        except ValueError as exc:
            raise ConfigError(
                f"cannot parse .f line: {exc}", code="CFG_INVALID", file=path, line=number
            ) from None
    return tokens

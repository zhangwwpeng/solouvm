"""Remote log prefilter keyword extraction (H-16, verdict.md §4.5)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.model import Checks, Expect

__all__ = [
    "BASELINE_KEYWORDS",
    "extract_keywords",
    "extract_literal",
    "keywords_for",
]

BASELINE_KEYWORDS: tuple[str, ...] = (
    "UVM_",
    "SOLOUVM_DONE",
    "TEST_DONE",
    "Report counts by severity",
    "uvm_test_top",
)

_MIN_LITERAL_LEN = 3
_QUANT_BRACE_RE = re.compile(r"\{(?:(\d+)|(\d*),(\d*))\}")
_ANCHOR_ESCAPES = frozenset({"A", "b", "B", "d", "D", "s", "S", "w", "W", "Z"})
_NON_LITERAL_ESCAPES = frozenset({"n", "r", "f", "v", "a"})


@dataclass(frozen=True)
class _Fragment:
    exact: str | None
    literals: tuple[str, ...]


_EMPTY_FRAG = _Fragment(exact=None, literals=())


class _CaseInsensitiveError(Exception):
    """Internal sentinel raised when global (?i) or (?x) invalidates literal extraction."""


def _common_substrings(a: str, b: str, min_len: int = _MIN_LITERAL_LEN) -> list[str]:
    if len(a) < min_len or len(b) < min_len:
        return []
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    found: set[str] = set()
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
                length = dp[i][j]
                if length >= min_len and (i == m or j == n or a[i] != b[j]):
                    found.add(a[i - length : i])
    return sorted(found)


def _intersect_branch_literals(branches: list[_Fragment]) -> _Fragment:
    if not branches:
        return _EMPTY_FRAG
    if len(branches) == 1:
        return branches[0]
    common = [lit for lit in dict.fromkeys(branches[0].literals) if len(lit) >= _MIN_LITERAL_LEN]
    for branch in branches[1:]:
        if not common or not branch.literals:
            return _EMPTY_FRAG
        next_common: set[str] = set()
        for left in common:
            for right in branch.literals:
                next_common.update(_common_substrings(left, right))
        common = sorted(next_common)
    return _Fragment(exact=None, literals=tuple(common))


def _concat_fragments(items: list[_Fragment]) -> _Fragment:
    if not items:
        return _Fragment(exact="", literals=())
    if all(item.exact is not None for item in items):
        combined = "".join(item.exact for item in items if item.exact is not None)
        return _Fragment(exact=combined, literals=(combined,) if combined else ())

    literals: list[str] = []
    run: list[str] = []
    for item in items:
        if item.exact is not None:
            run.append(item.exact)
        else:
            if run:
                merged = "".join(run)
                if merged:
                    literals.append(merged)
                run.clear()
            literals.extend(item.literals)
    if run:
        merged = "".join(run)
        if merged:
            literals.append(merged)
    return _Fragment(exact=None, literals=tuple(dict.fromkeys(literals)))


class _RegexParser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.pos = 0
        self.length = len(pattern)

    def parse(self) -> _Fragment:
        frag = self._parse_alternation()
        return frag

    def _parse_alternation(self) -> _Fragment:
        branches = [self._parse_sequence()]
        while self.pos < self.length and self.pattern[self.pos] == "|":
            self.pos += 1
            branches.append(self._parse_sequence())
        return _intersect_branch_literals(branches)

    def _parse_sequence(self) -> _Fragment:
        items: list[_Fragment] = []
        while self.pos < self.length:
            ch = self.pattern[self.pos]
            if ch in (")", "|"):
                break
            atom = self._parse_atom()
            atom = self._apply_quantifier(atom)
            items.append(atom)
        return _concat_fragments(items)

    def _parse_atom(self) -> _Fragment:
        ch = self.pattern[self.pos]
        if ch == "(":
            return self._parse_group()
        if ch == "[":
            self._skip_char_class()
            return _EMPTY_FRAG
        if ch in (".", "^", "$"):
            self.pos += 1
            return _EMPTY_FRAG
        if ch == "\\":
            return self._parse_escape()
        self.pos += 1
        return _Fragment(exact=ch, literals=(ch,))

    def _parse_escape(self) -> _Fragment:
        self.pos += 1
        if self.pos >= self.length:
            return _EMPTY_FRAG
        esc = self.pattern[self.pos]
        self.pos += 1
        if esc == "t":
            return _Fragment(exact="\t", literals=("\t",))
        if esc == "g" and self.pos < self.length and self.pattern[self.pos] == "<":
            end = self.pattern.find(">", self.pos + 1)
            self.pos = end + 1 if end != -1 else self.length
            return _EMPTY_FRAG
        if esc == "N" and self.pos < self.length and self.pattern[self.pos] == "{":
            end = self.pattern.find("}", self.pos + 1)
            self.pos = end + 1 if end != -1 else self.length
            return _EMPTY_FRAG
        if esc in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[esc]
            self.pos = min(self.length, self.pos + width)
            return _EMPTY_FRAG
        if esc.isdigit():
            for _ in range(2):
                if self.pos < self.length and self.pattern[self.pos].isdigit():
                    self.pos += 1
                else:
                    break
            return _EMPTY_FRAG
        if esc in _ANCHOR_ESCAPES or esc in _NON_LITERAL_ESCAPES or esc == "k":
            return _EMPTY_FRAG
        return _Fragment(exact=esc, literals=(esc,))

    def _skip_char_class(self) -> None:
        self.pos += 1  # consume '['
        if self.pos < self.length and self.pattern[self.pos] == "^":
            self.pos += 1
        if self.pos < self.length and self.pattern[self.pos] == "]":
            self.pos += 1
        while self.pos < self.length:
            ch = self.pattern[self.pos]
            if ch == "\\":
                self.pos += 2
                continue
            self.pos += 1
            if ch == "]":
                break

    def _parse_group(self) -> _Fragment:
        self.pos += 1  # consume '('
        if self.pos >= self.length:
            return _EMPTY_FRAG
        if self.pattern[self.pos] != "?":
            inner = self._parse_alternation()
            if self.pos < self.length and self.pattern[self.pos] == ")":
                self.pos += 1
            return inner

        self.pos += 1  # consume '?'
        if self.pos >= self.length:
            return _EMPTY_FRAG
        nxt = self.pattern[self.pos]

        # Comment (?#...)
        if nxt == "#":
            while self.pos < self.length and self.pattern[self.pos] != ")":
                self.pos += 1
            if self.pos < self.length:
                self.pos += 1
            return _EMPTY_FRAG

        # Non-capturing (?:...) or atomic (?>...)
        if nxt in (":", ">"):
            self.pos += 1
            inner = self._parse_alternation()
            if self.pos < self.length and self.pattern[self.pos] == ")":
                self.pos += 1
            return inner

        # Named group (?P<name>...) or backref (?P=name)
        if nxt == "P":
            self.pos += 1
            if self.pos < self.length and self.pattern[self.pos] == "<":
                end_name = self.pattern.find(">", self.pos + 1)
                if end_name != -1:
                    self.pos = end_name + 1
                    inner = self._parse_alternation()
                    if self.pos < self.length and self.pattern[self.pos] == ")":
                        self.pos += 1
                    return inner
            self._skip_balanced_group()
            return _EMPTY_FRAG

        # Lookahead / lookbehind / conditional: zero-width or conservative fallback
        if nxt in ("=", "!", "<", "("):
            self._skip_balanced_group()
            return _EMPTY_FRAG

        # Inline flags (?aiLmsux) or scoped (?flags:...)
        flag_start = self.pos
        while self.pos < self.length and self.pattern[self.pos] not in (":", ")"):
            self.pos += 1
        flags_text = self.pattern[flag_start : self.pos]
        pos_flags = flags_text.split("-", 1)[0]
        has_ix = "i" in pos_flags or "x" in pos_flags

        if self.pos < self.length and self.pattern[self.pos] == ":":
            self.pos += 1
            inner = self._parse_alternation()
            if self.pos < self.length and self.pattern[self.pos] == ")":
                self.pos += 1
            return _EMPTY_FRAG if has_ix else inner

        if self.pos < self.length and self.pattern[self.pos] == ")":
            self.pos += 1
        if has_ix:
            raise _CaseInsensitiveError
        return _EMPTY_FRAG

    def _skip_balanced_group(self) -> None:
        depth = 1
        while self.pos < self.length and depth > 0:
            ch = self.pattern[self.pos]
            if ch == "\\":
                self.pos += 2
            elif ch == "[":
                self._skip_char_class()
            elif ch == "(":
                depth += 1
                self.pos += 1
            elif ch == ")":
                depth -= 1
                self.pos += 1
            else:
                self.pos += 1

    def _apply_quantifier(self, atom: _Fragment) -> _Fragment:
        while self.pos < self.length:
            ch = self.pattern[self.pos]
            min_rep: int
            max_rep: int | None
            if ch == "*":
                min_rep, max_rep = 0, None
                self.pos += 1
            elif ch == "?":
                min_rep, max_rep = 0, 1
                self.pos += 1
            elif ch == "+":
                min_rep, max_rep = 1, None
                self.pos += 1
            elif ch == "{":
                match = _QUANT_BRACE_RE.match(self.pattern, self.pos)
                if match is None:
                    break
                if match.group(1) is not None:
                    min_rep = max_rep = int(match.group(1))
                else:
                    min_rep = int(match.group(2)) if match.group(2) else 0
                    max_rep = int(match.group(3)) if match.group(3) else None
                self.pos = match.end()
            else:
                break

            # Consume optional lazy '?' or possessive '+' modifier
            if self.pos < self.length and self.pattern[self.pos] in ("?", "+"):
                self.pos += 1

            if min_rep == 0:
                atom = _EMPTY_FRAG
            elif min_rep == max_rep:
                if atom.exact is not None:
                    exact = atom.exact * min_rep
                    atom = _Fragment(exact=exact, literals=(exact,) if exact else ())
                else:
                    atom = _Fragment(exact=None, literals=atom.literals)
            elif atom.exact is not None:
                repeated = atom.exact * min_rep
                atom = _Fragment(exact=None, literals=(repeated,) if repeated else ())
            else:
                atom = _Fragment(exact=None, literals=atom.literals)
        return atom


def extract_literal(pattern: str) -> str | None:
    """Return a >=3-char literal substring guaranteed in every match of ``pattern``, or None."""
    if not isinstance(pattern, str) or not pattern:
        return None
    try:
        re.compile(pattern)
    except re.error:
        return None
    try:
        frag = _RegexParser(pattern).parse()
    except _CaseInsensitiveError:
        return None
    candidates = [lit for lit in frag.literals if len(lit) >= _MIN_LITERAL_LEN]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (len(s), s))


def extract_keywords(
    backend: Backend,
    checks: Checks,
    expects: Iterable[Expect] = (),
    *,
    today: date | None = None,
) -> list[str] | None:
    """Build sorted remote ``grep -F`` keywords for a job, or None if full log is needed."""
    keywords: set[str] = set(BASELINE_KEYWORDS)

    patterns: list[str] = list(backend.error_regex)
    env_patterns = (
        checks.env_fail_regex if checks.env_fail_regex is not None else backend.license_regex
    )
    patterns.extend(env_patterns)
    patterns.extend(checks.fail_regex)
    patterns.extend(checks.pass_regex)
    if backend.time_regex:
        patterns.append(backend.time_regex)

    ref_date = today if today is not None else date.today()
    for waiver in checks.waivers:
        if waiver.until is not None and date.fromisoformat(waiver.until) < ref_date:
            continue
        if waiver.regex is not None:
            patterns.append(waiver.regex)

    for expect in expects:
        if expect.regex is not None:
            patterns.append(expect.regex)
        if expect.scoreboard is not None:
            keywords.add("compared=")

    for pattern in patterns:
        lit = extract_literal(pattern)
        if lit is None:
            return None
        keywords.add(lit)

    return sorted(keywords)


keywords_for = extract_keywords

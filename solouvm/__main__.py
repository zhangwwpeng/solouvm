"""Module entrypoint for ``python -m solouvm``."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Delegate execution to ``solouvm.cli.main``."""
    from solouvm.cli import main as cli_main

    return cli_main(list(argv) if argv is not None else None)


if __name__ == "__main__":
    sys.exit(main())

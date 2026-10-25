"""solouvm: generate UVM verification frameworks and runners from Python config.

Public entry points live in :mod:`solouvm.cli`; user-facing configuration types
are re-exported here so that ``case.py`` files only need ``from solouvm import Case``.
"""

from .errors import ConfigError, InfraError, SolouvmError
from .model import EXEC_STATUSES, STATUSES, Case

__version__ = "0.1.0"

__all__ = [
    "EXEC_STATUSES",
    "STATUSES",
    "Case",
    "ConfigError",
    "InfraError",
    "SolouvmError",
    "__version__",
]

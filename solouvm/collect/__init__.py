"""File list and case collection: ``filelist.py``, ``case.py`` and ``case_dirs``."""

from solouvm.collect.cases import (
    CaseSource,
    case_file_for,
    collect_cases,
    collect_cases_with_sources,
)
from solouvm.collect.dotf import parse_dotf
from solouvm.collect.files import (
    DEFAULT_FILELIST_PRIORITY,
    GROUPS,
    PACKAGE_GROUPS,
    FileEntry,
    FileList,
    LoadedFilelist,
    collect_files,
    load_filelist,
    load_filelist_meta,
)

__all__ = [
    "DEFAULT_FILELIST_PRIORITY",
    "GROUPS",
    "PACKAGE_GROUPS",
    "CaseSource",
    "FileEntry",
    "FileList",
    "LoadedFilelist",
    "case_file_for",
    "collect_cases",
    "collect_cases_with_sources",
    "collect_files",
    "load_filelist",
    "load_filelist_meta",
    "parse_dotf",
]

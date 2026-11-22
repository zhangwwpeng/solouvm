"""UVM environment generator, region merger, package list maintainer, and linter."""

from __future__ import annotations

from pathlib import Path

from solouvm.config.load import load_config
from solouvm.errors import ConfigError
from solouvm.model import Case, Expect
from solouvm.records.write import add_case, record_lock
from solouvm.uvmgen.lint import StyleIssue, lint_style
from solouvm.uvmgen.pkgfiles import (
    expected_pkg_files,
    find_svh_inconsistencies,
    render_pkg_files,
    update_pkg_files,
)
from solouvm.uvmgen.regions import (
    compute_checksum,
    finalize_checksum,
    merge_regions,
    parse_header,
    user_regions,
)
from solouvm.uvmgen.render import render_component, render_tb_top
from solouvm.uvmgen.spec import (
    Agent,
    Env,
    Field,
    Interface,
    Signal,
    Tbspec,
    load_tbspec,
)
from solouvm.uvmgen.sync import SyncReport, uvm_diff, uvm_sync

__all__ = [
    "Agent",
    "Env",
    "Field",
    "Interface",
    "Signal",
    "StyleIssue",
    "SyncReport",
    "Tbspec",
    "compute_checksum",
    "create_component",
    "expected_pkg_files",
    "finalize_checksum",
    "find_svh_inconsistencies",
    "lint_style",
    "load_tbspec",
    "merge_regions",
    "parse_header",
    "render_component",
    "render_pkg_files",
    "render_tb_top",
    "update_pkg_files",
    "user_regions",
    "uvm_diff",
    "uvm_sync",
]


def _resolve_case_target(root: Path, case_dirs: list[str], case_path: Path | None) -> Path:
    if case_path is not None:
        return Path(case_path)
    if case_dirs:
        first = Path(case_dirs[0])
        target = first if first.is_absolute() else root / first
        target.mkdir(parents=True, exist_ok=True)
        return target
    return root / "case.py"


def create_component(
    root: Path,
    kind: str,
    name: str,
    *,
    env_name: str | None = None,
    agent_name: str | None = None,
    agents: list[str] | None = None,
    templates_dir: Path | None = None,
    case_path: Path | None = None,
) -> list[Path]:
    """Create UVM component files for ``kind`` without overwriting existing files.

    Raises ``ConfigError(..., code="UVMGEN_EXISTS")`` listing every conflicting path before
    writing anything. Updates ``<pkg>_files.svh`` and, for ``kind == "test"``, registers a
    ``stage="wip"`` case with ``Expect(scoreboard="<ENV>_SCB", min=1)``.
    """
    root_path = Path(root)
    spec = load_tbspec(root_path) if (root_path / "tbspec.py").is_file() else None

    case_dirs: list[str] = []
    conf_path = root_path / "solouvm.conf.py"
    if conf_path.is_file():
        cfg = load_config(conf_path)
        case_dirs = list(cfg.case_dirs)
        if templates_dir is None and cfg.uvm.templates is not None:
            tpath = Path(cfg.uvm.templates)
            templates_dir = tpath if tpath.is_absolute() else root_path / tpath

    rendered = render_component(
        kind,
        name,
        spec,
        root=root_path,
        env_name=env_name,
        agent_name=agent_name,
        agents=agents,
        templates_dir=templates_dir,
    )

    conflicts: list[Path] = []
    to_write: dict[Path, str] = {}
    for path, content in rendered.items():
        if path.exists():
            is_shared_if = (
                kind == "agent" and path.name.endswith("_if.sv") and path.parent.name != name
            )
            if is_shared_if:
                existing = path.read_text(encoding="utf-8")
                if existing != content:
                    merged_if, _, _ = merge_regions(content, existing)
                    if merged_if != existing:
                        to_write[path] = merged_if
                continue
            conflicts.append(path)
            continue
        to_write[path] = content

    if conflicts:
        rel_conflicts = sorted(p.relative_to(root_path).as_posix() for p in conflicts)
        raise ConfigError(
            "refusing to overwrite existing file(s): " + ", ".join(rel_conflicts),
            code="UVMGEN_EXISTS",
        )

    written: list[Path] = []
    for path, content in sorted(to_write.items(), key=lambda kv: kv[0].as_posix()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)

    if kind == "agent":
        svh = update_pkg_files(root_path, name)
        if svh not in written:
            written.append(svh)
    elif kind == "env":
        for pkg in (f"{name}_env", f"{name}_test"):
            svh = update_pkg_files(root_path, pkg)
            if svh not in written:
                written.append(svh)
    elif kind == "seq":
        assert agent_name is not None
        svh = update_pkg_files(root_path, agent_name)
        written.append(svh)
    elif kind == "vseq":
        assert env_name is not None
        svh = update_pkg_files(root_path, f"{env_name}_env")
        written.append(svh)
    elif kind == "test":
        assert env_name is not None
        svh = update_pkg_files(root_path, f"{env_name}_test")
        written.append(svh)
        target = _resolve_case_target(root_path, case_dirs, case_path)
        new_case = Case(
            name=name,
            uvm_test=f"{env_name}_{name}_test",
            stage="wip",
            expect=[Expect(scoreboard=f"{env_name.upper()}_SCB", min=1)],
        )
        with record_lock(root_path):
            add_case(target, new_case)
        case_file = target / f"{name}.py" if target.is_dir() else target
        if case_file not in written:
            written.append(case_file)

    return sorted(set(written), key=lambda p: p.as_posix())

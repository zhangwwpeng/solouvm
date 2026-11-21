"""Synchronize generated UVM files with ``tbspec.py`` and templates (U-04, U-05, U-10)."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from solouvm.config.load import load_config
from solouvm.errors import ConfigError
from solouvm.uvmgen.pkgfiles import pkg_files_relpath, render_pkg_files
from solouvm.uvmgen.regions import merge_regions, parse_header
from solouvm.uvmgen.render import render_component, render_tb_top
from solouvm.uvmgen.spec import Tbspec, load_tbspec

__all__ = ["SyncReport", "uvm_diff", "uvm_sync"]


@dataclass
class SyncReport:
    """Summary of changes (or planned changes in ``dry_run``) from ``uvm_sync``."""

    updated: list[Path] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    orphans: list[Path] = field(default_factory=list)
    modified_outside: list[Path] = field(default_factory=list)
    extra_files: list[Path] = field(default_factory=list)
    diff: str = ""
    dry_run: bool = False


def _resolve_templates_dir(root: Path, templates_dir: Path | None) -> Path | None:
    if templates_dir is not None:
        return templates_dir
    conf_path = root / "solouvm.conf.py"
    if not conf_path.is_file():
        return None
    cfg = load_config(conf_path)
    if cfg.uvm.templates is None:
        return None
    tpath = Path(cfg.uvm.templates)
    return tpath if tpath.is_absolute() else root / tpath


def _discover_user_items(
    root: Path,
    spec: Tbspec,
    *,
    templates_dir: Path | None,
    target_agents: set[str],
    target_envs: set[str],
) -> tuple[dict[Path, str], set[Path]]:
    """Discover existing seq/vseq/test files under ``verif/`` and render matching ones."""
    verif = root / "verif"
    extra_rendered: dict[Path, str] = {}
    known_user_paths: set[Path] = set()

    for ag in spec.agents:
        seq_dir = verif / "agents" / ag.name / "seq"
        if not seq_dir.is_dir():
            continue
        prefix = f"{ag.name}_"
        base_name = f"{ag.name}_base_seq.sv"
        for sv in sorted(seq_dir.glob(f"{prefix}*_seq.sv")):
            if not sv.is_file() or sv.name == base_name:
                continue
            rel = sv.relative_to(root)
            known_user_paths.add(rel)
            if ag.name in target_agents:
                seq_name = sv.stem.removeprefix(prefix).removesuffix("_seq")
                hdr = parse_header(sv.read_text(encoding="utf-8"))
                if hdr is not None and hdr[1] == "agent_seq" and seq_name:
                    extra_rendered.update(
                        render_component(
                            "seq",
                            seq_name,
                            spec,
                            agent_name=ag.name,
                            templates_dir=templates_dir,
                        )
                    )

    for env_spec in spec.envs:
        vseq_dir = verif / "envs" / env_spec.name / "vseq"
        if vseq_dir.is_dir():
            prefix = f"{env_spec.name}_"
            base_vseq = f"{env_spec.name}_base_vseq.sv"
            for sv in sorted(vseq_dir.glob(f"{prefix}*_vseq.sv")):
                if not sv.is_file() or sv.name == base_vseq:
                    continue
                rel = sv.relative_to(root)
                known_user_paths.add(rel)
                if env_spec.name in target_envs:
                    vseq_name = sv.stem.removeprefix(prefix).removesuffix("_vseq")
                    hdr = parse_header(sv.read_text(encoding="utf-8"))
                    if hdr is not None and hdr[1] == "env_vseq" and vseq_name:
                        extra_rendered.update(
                            render_component(
                                "vseq",
                                vseq_name,
                                spec,
                                env_name=env_spec.name,
                                templates_dir=templates_dir,
                            )
                        )

        test_dir = verif / "tests" / env_spec.name
        if test_dir.is_dir():
            prefix = f"{env_spec.name}_"
            base_test = f"{env_spec.name}_base_test.sv"
            for sv in sorted(test_dir.glob(f"{prefix}*_test.sv")):
                if not sv.is_file() or sv.name == base_test:
                    continue
                rel = sv.relative_to(root)
                known_user_paths.add(rel)
                if env_spec.name in target_envs:
                    test_name = sv.stem.removeprefix(prefix).removesuffix("_test")
                    hdr = parse_header(sv.read_text(encoding="utf-8"))
                    if hdr is not None and hdr[1] == "test" and test_name:
                        extra_rendered.update(
                            render_component(
                                "test",
                                test_name,
                                spec,
                                env_name=env_spec.name,
                                templates_dir=templates_dir,
                            )
                        )

    return extra_rendered, known_user_paths


def _merge_svh_entries(
    rendered: dict[Path, str],
    root: Path,
    spec: Tbspec,
    known_user_paths: set[Path],
) -> None:
    """Ensure user seq/vseq/test files on disk remain included in regenerated ``*_files.svh``."""
    verif = root / "verif"
    for ag in spec.agents:
        svh_rel = pkg_files_relpath(ag.name)
        if svh_rel not in rendered:
            continue
        pkg_files = [
            p.relative_to("verif").as_posix()
            for p in (*rendered, *known_user_paths)
            if p.parts[:3] == ("verif", "agents", ag.name)
            and p.suffix == ".sv"
            and not p.name.endswith("_pkg.sv")
            and not p.name.endswith("_if.sv")
        ]
        # Also include any existing .sv files in the agent dir if not already listed.
        ag_dir = verif / "agents" / ag.name
        if ag_dir.is_dir():
            for sv in ag_dir.rglob("*.sv"):
                if sv.name.endswith("_pkg.sv") or sv.name.endswith("_if.sv"):
                    continue
                rel_v = sv.relative_to(verif).as_posix()
                if rel_v not in pkg_files:
                    pkg_files.append(rel_v)
        rendered[svh_rel] = render_pkg_files(ag.name, pkg_files)

    for env_spec in spec.envs:
        env_pkg = f"{env_spec.name}_env"
        env_svh_rel = pkg_files_relpath(env_pkg)
        if env_svh_rel in rendered:
            env_files = [
                p.relative_to("verif").as_posix()
                for p in (*rendered, *known_user_paths)
                if p.parts[:3] == ("verif", "envs", env_spec.name)
                and p.suffix == ".sv"
                and not p.name.endswith("_pkg.sv")
            ]
            env_dir = verif / "envs" / env_spec.name
            if env_dir.is_dir():
                for sv in env_dir.rglob("*.sv"):
                    if sv.name.endswith("_pkg.sv"):
                        continue
                    rel_v = sv.relative_to(verif).as_posix()
                    if rel_v not in env_files:
                        env_files.append(rel_v)
            rendered[env_svh_rel] = render_pkg_files(env_pkg, env_files)

        test_pkg = f"{env_spec.name}_test"
        test_svh_rel = pkg_files_relpath(test_pkg)
        if test_svh_rel in rendered:
            test_files = [
                p.relative_to("verif").as_posix()
                for p in (*rendered, *known_user_paths)
                if p.parts[:3] == ("verif", "tests", env_spec.name)
                and p.suffix == ".sv"
                and not p.name.endswith("_pkg.sv")
            ]
            test_dir = verif / "tests" / env_spec.name
            if test_dir.is_dir():
                for sv in test_dir.rglob("*.sv"):
                    if sv.name.endswith("_pkg.sv"):
                        continue
                    rel_v = sv.relative_to(verif).as_posix()
                    if rel_v not in test_files:
                        test_files.append(rel_v)
            rendered[test_svh_rel] = render_pkg_files(test_pkg, test_files)


def _format_orphan(orphans: dict[str, str]) -> str:
    blocks = []
    for name in sorted(orphans):
        body = orphans[name]
        if body and not body.endswith("\n"):
            body += "\n"
        blocks.append(f"// solouvm:user begin {name}\n{body}// solouvm:user end {name}\n")
    return "\n".join(blocks)


def _merge_orphan_text(existing_text: str | None, orphans: dict[str, str]) -> str:
    blocks = [_format_orphan({name: orphans[name]}) for name in sorted(orphans)]
    if not existing_text or not existing_text.strip():
        return "\n".join(blocks)
    to_append = [blk for blk in blocks if blk.rstrip("\n") not in existing_text]
    if not to_append:
        return existing_text
    return existing_text.rstrip("\n") + "\n\n" + "\n".join(to_append)


def _unified_diff(rel: Path, old_text: str | None, new_text: str) -> str:
    rel_posix = rel.as_posix()
    if old_text is None:
        old_lines: list[str] = []
        fromfile = "/dev/null"
    else:
        old_lines = old_text.splitlines(keepends=True)
        fromfile = f"a/{rel_posix}"
    new_lines = new_text.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=fromfile,
            tofile=f"b/{rel_posix}",
        )
    )


def uvm_sync(
    root: Path,
    *,
    env: str | None = None,
    agent: str | None = None,
    dry_run: bool = False,
    templates_dir: Path | None = None,
) -> SyncReport:
    """Synchronize generated UVM components under ``root`` with ``tbspec.py``."""
    root_path = Path(root)
    spec = load_tbspec(root_path)
    resolved_templates = _resolve_templates_dir(root_path, templates_dir)

    if env is not None and spec.get_env(env) is None:
        raise ConfigError(f"env {env!r} not found in tbspec.py", code="CFG_INVALID")
    if agent is not None and spec.get_agent(agent) is None:
        raise ConfigError(f"agent {agent!r} not found in tbspec.py", code="CFG_INVALID")
    if env is not None and agent is not None:
        env_obj = spec.get_env(env)
        assert env_obj is not None
        if not any(ag.name == agent for ag in env_obj.agents):
            raise ConfigError(
                f"agent {agent!r} does not belong to env {env!r}",
                code="CFG_INVALID",
            )

    all_rendered: dict[Path, str] = {}
    for ag in spec.agents:
        all_rendered.update(
            render_component("agent", ag.name, spec, templates_dir=resolved_templates)
        )
    for env_obj in spec.envs:
        all_rendered.update(
            render_component("env", env_obj.name, spec, templates_dir=resolved_templates)
        )
    if spec.envs:
        all_rendered.update(render_tb_top(spec.envs, templates_dir=resolved_templates))

    if agent is not None:
        target_agents = {agent}
        target_envs: set[str] = set()
        target_rendered = render_component("agent", agent, spec, templates_dir=resolved_templates)
    elif env is not None:
        env_obj = spec.get_env(env)
        assert env_obj is not None
        target_agents = {ag.name for ag in env_obj.agents}
        target_envs = {env_obj.name}
        target_rendered = {}
        for ag in env_obj.agents:
            target_rendered.update(
                render_component("agent", ag.name, spec, templates_dir=resolved_templates)
            )
        target_rendered.update(
            render_component("env", env_obj.name, spec, templates_dir=resolved_templates)
        )
        target_rendered.update(render_tb_top(spec.envs, templates_dir=resolved_templates))
    else:
        target_agents = {ag.name for ag in spec.agents}
        target_envs = {e.name for e in spec.envs}
        target_rendered = dict(all_rendered)

    extra_user_rendered, known_user_paths = _discover_user_items(
        root_path,
        spec,
        templates_dir=resolved_templates,
        target_agents=target_agents,
        target_envs=target_envs,
    )
    target_rendered.update(extra_user_rendered)
    _merge_svh_entries(target_rendered, root_path, spec, known_user_paths)

    report = SyncReport(dry_run=dry_run)
    diff_chunks: list[str] = []

    for rel in sorted(target_rendered, key=lambda p: p.as_posix()):
        new_text = target_rendered[rel]
        abs_path = root_path / rel
        if not abs_path.exists():
            report.created.append(rel)
            chunk = _unified_diff(rel, None, new_text)
            if chunk:
                diff_chunks.append(chunk)
            if not dry_run:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(new_text, encoding="utf-8")
            continue

        old_text = abs_path.read_text(encoding="utf-8")
        if rel.suffix == ".svh":
            if new_text == old_text:
                report.unchanged.append(rel)
            else:
                report.updated.append(rel)
                chunk = _unified_diff(rel, old_text, new_text)
                if chunk:
                    diff_chunks.append(chunk)
                if not dry_run:
                    abs_path.write_text(new_text, encoding="utf-8")
            continue

        merged_text, orphan_dict, modified_outside = merge_regions(new_text, old_text)
        if modified_outside:
            report.modified_outside.append(rel)
            chunk = _unified_diff(rel, old_text, merged_text)
            if chunk:
                diff_chunks.append(chunk)
            continue

        if orphan_dict:
            orphan_rel = Path(f"{rel.as_posix()}.orphan")
            report.orphans.append(orphan_rel)
            if not dry_run:
                orphan_abs = root_path / orphan_rel
                existing_orphan = (
                    orphan_abs.read_text(encoding="utf-8") if orphan_abs.is_file() else None
                )
                orphan_abs.write_text(
                    _merge_orphan_text(existing_orphan, orphan_dict),
                    encoding="utf-8",
                )

        if merged_text == old_text:
            report.unchanged.append(rel)
        else:
            report.updated.append(rel)
            chunk = _unified_diff(rel, old_text, merged_text)
            if chunk:
                diff_chunks.append(chunk)
            if not dry_run:
                abs_path.write_text(merged_text, encoding="utf-8")

    expected_all = set(all_rendered) | known_user_paths
    verif_dir = root_path / "verif"
    if verif_dir.is_dir():
        for existing in sorted(verif_dir.rglob("*"), key=lambda p: p.as_posix()):
            if not existing.is_file() or existing.suffix == ".orphan":
                continue
            rel = existing.relative_to(root_path)
            if rel not in expected_all:
                report.extra_files.append(rel)

    report.diff = "".join(diff_chunks)
    return report


def uvm_diff(
    root: Path,
    *,
    env: str | None = None,
    agent: str | None = None,
    templates_dir: Path | None = None,
) -> str:
    """Return a unified diff previewing ``uvm_sync`` changes without writing files."""
    return uvm_sync(
        root,
        env=env,
        agent=agent,
        dry_run=True,
        templates_dir=templates_dir,
    ).diff

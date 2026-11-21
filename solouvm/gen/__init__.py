"""Code/Makefile/filelist/script generation for B-side execution (architecture.md §4-§5)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.backends import fake_sim_source
from solouvm.gen.filelist import write_filelists
from solouvm.gen.makefile import (
    PLACEHOLDERS,
    expand_template,
    render_entry_makefile,
    render_root_makefile,
    render_run_mk,
)
from solouvm.gen.scripts import LAUNCH_SH, RUN_JOB_SH, render_launch, render_run_job
from solouvm.gen.shell import make_escape, relpath, sh_quote

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config
    from solouvm.model import Plan

__all__ = [
    "LAUNCH_SH",
    "PLACEHOLDERS",
    "RUN_JOB_SH",
    "expand_template",
    "make_escape",
    "relpath",
    "render_entry_makefile",
    "render_launch",
    "render_root_makefile",
    "render_run_job",
    "render_run_mk",
    "sh_quote",
    "write_filelists",
    "write_generated_project_files",
]


def _write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def write_generated_project_files(
    *,
    config: Config,
    plan: Plan,
    files: FileList,
    backend: Backend,
    build_keys: dict[str, str] | None = None,
    version: str = "0.1.0",
) -> list[str]:
    """Write all generated B-side files and return sorted root-relative push manifest paths."""
    root = Path(config.root)
    keys = dict(plan.builds if build_keys is None else build_keys)
    manifest: list[str] = []

    scripts = (
        (".solouvm/gen/bin/run_job.sh", render_run_job(version)),
        (".solouvm/gen/bin/launch.sh", render_launch(version)),
        (".solouvm/gen/bin/fake_sim.sh", fake_sim_source()),
    )
    for rel_path, content in scripts:
        _write_text(root / rel_path, content, mode=0o755)
        manifest.append(rel_path)

    entry_mk_rel = ".solouvm/Makefile"
    _write_text(root / entry_mk_rel, render_entry_makefile(version))
    manifest.append(entry_mk_rel)

    root_makefile = root / "Makefile"
    if not root_makefile.exists():
        _write_text(root_makefile, render_root_makefile(version))

    build_dirs = {mode: f".solouvm/build/{mode}/{key}" for mode, key in sorted(keys.items())}
    manifest.extend(write_filelists(root, plan, files, build_dirs, config=config))

    run_mk_rel = f".solouvm/runs/{plan.run_id}/run.mk"
    run_mk_text = render_run_mk(
        plan=plan,
        config=config,
        files=files,
        build_keys=keys,
        backend=backend,
        version=version,
    )
    _write_text(root / run_mk_rel, run_mk_text)
    manifest.append(run_mk_rel)

    return sorted(manifest)

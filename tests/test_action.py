"""action.yml's install step, run the way GitHub runs composite steps (bash -e -o pipefail), with uvx stubbed."""
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def run_install(action_path: Path, tmp_path: Path) -> tuple[subprocess.CompletedProcess, str, str]:
    steps = yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]
    script = next(s for s in steps if s.get("id") == "install")["run"]
    bin_dir, out, calls = tmp_path / "bin", tmp_path / "github_output", tmp_path / "uvx_calls"
    bin_dir.mkdir()
    (bin_dir / "uvx").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    (bin_dir / "uvx").chmod(0o755)
    out.touch()
    calls.touch()
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GITHUB_ACTION_PATH": str(action_path),
           "GITHUB_OUTPUT": str(out), "INPUT_BROWSERS": "chromium"}
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                          env=env, capture_output=True, text=True)
    return proc, out.read_text(), calls.read_text()


def test_install_builds_from_the_source_when_there_is_no_wheel(tmp_path):
    action = tmp_path / "action"
    action.mkdir()
    proc, output, calls = run_install(action, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert output.strip() == f"from={action}"
    assert f"--from {action} assay browsers --with-deps chromium" in calls


def test_install_prefers_the_newest_wheel_in_dist(tmp_path):
    action = tmp_path / "action"
    (action / "dist").mkdir(parents=True)
    for v in ("1.1.0", "1.2.0"):
        (action / "dist" / f"assay-{v}-py3-none-any.whl").touch()
    proc, output, _ = run_install(action, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert output.strip() == f"from={action}/dist/assay-1.2.0-py3-none-any.whl"

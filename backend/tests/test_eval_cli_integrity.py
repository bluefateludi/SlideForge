"""CLI 模式冲突和无效基线必须在调用模型之前结束。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"


def invoke(args, *, gate_env=""):
    env = {**os.environ, "EVAL_GATE": gate_env, "LLM_API_KEY": ""}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


@pytest.mark.parametrize(
    "args,gate_env",
    [
        (["--gate", "--baseline"], ""),
        (["--gate", "--baseline-from-db"], ""),
        (["--baseline", "--baseline-from-db"], ""),
        (["--baseline"], "1"),
        (["--baseline-from-db"], "1"),
    ],
)
def test_conflicting_modes_fail_before_evaluation(args, gate_env):
    result = invoke(args, gate_env=gate_env)
    assert result.returncode == 2
    assert "互斥" in result.stderr
    assert "冒烟" not in result.stderr


def test_invalid_baseline_fails_before_evaluation(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text('{"status":"needs_rebuild"}', encoding="utf-8")
    original = path.read_bytes()
    result = invoke(["--gate", "--baseline-file", str(path)])
    assert result.returncode == 1
    assert "基线" in result.stderr
    assert "冒烟" not in result.stderr
    assert path.read_bytes() == original


def test_cases_filter_is_preserved():
    result = invoke(["--help"])
    assert result.returncode == 0
    assert "--cases" in result.stdout
    assert "--baseline-file" in result.stdout

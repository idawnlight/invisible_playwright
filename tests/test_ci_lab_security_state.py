"""scripts/ci_lab_security_state.py: a CI leg launches with a fixed, ready security state that the repository holds, and the core is told so."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("ci_lab_security_state_under_test", ROOT / "scripts" / "ci_lab_security_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lab = _load()
PIN = ROOT / "ci" / "security-state.pin.json"
RESULT = ROOT / "ci" / "security-state" / "securitystate-result.json"
RUN = "python scripts/ci_lab_security_state.py set --pin ci/security-state.pin.json"


def test_the_pin_is_the_hash_of_the_file_it_names():
    pin = lab.load_pin(PIN)
    assert pin["file"] == "ci/security-state/securitystate-result.json" and pin["comment"]
    assert hashlib.sha256(RESULT.read_bytes()).hexdigest() == pin["sha256"]
    assert len(RESULT.read_bytes()) == pin["bytes"]


def test_the_directory_is_a_ready_state_with_no_data():
    """A result with no data: ok, no engine, zero timestamps, no CRLite hashes; and no `security_state/`, so a launch copies nothing."""
    result = json.loads(RESULT.read_bytes())
    assert result["format"] == 1 and result["ok"] is True and result["error"] is None
    assert result["engine"] == {"version": "", "build_id": ""}
    assert result["crlite_filter_hashes"] == []
    assert all(c["collection_timestamp"] == 0 for c in result["collections"].values())
    assert sorted(p.name for p in RESULT.parent.iterdir()) == ["securitystate-result.json"]


def test_set_writes_the_variable_with_the_absolute_directory_to_github_env(tmp_path, monkeypatch):
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    assert lab.main(["set", "--pin", str(PIN)]) == 0
    assert env_file.read_text(encoding="utf-8") == "STEALTHFOX_SECURITY_STATE=%s\n" % RESULT.parent.resolve()
    assert Path(RESULT.parent.resolve()).is_absolute()


def test_without_github_env_it_prints_the_line(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    assert lab.main(["set", "--pin", str(PIN)]) == 0
    assert capsys.readouterr().out == "STEALTHFOX_SECURITY_STATE=%s\n" % RESULT.parent.resolve()


def _copy_of_the_state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    shutil.copy(RESULT, directory / RESULT.name)
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"file": str(directory / RESULT.name), "sha256": lab.load_pin(PIN)["sha256"]}), encoding="utf-8")
    return directory, pin


def test_a_file_that_is_not_the_pinned_one_is_refused_and_exports_nothing(tmp_path, monkeypatch, capsys):
    directory, pin = _copy_of_the_state(tmp_path)
    (directory / RESULT.name).write_bytes(RESULT.read_bytes() + b"\n")
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    assert lab.main(["set", "--pin", str(pin)]) == 1
    assert not env_file.exists()
    assert "change both in one commit" in capsys.readouterr().err


def test_a_directory_that_holds_anything_else_is_refused_and_exports_nothing(tmp_path, monkeypatch, capsys):
    """Data in the directory would be copied into every profile of the leg: the leg launches with NO data, and the pin only vouches for the one file."""
    directory, pin = _copy_of_the_state(tmp_path)
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    assert lab.main(["set", "--pin", str(pin)]) == 0
    env_file.unlink()
    (directory / "security_state").mkdir()
    (directory / "security_state" / "data.safe.bin").write_bytes(b"x")
    assert lab.main(["set", "--pin", str(pin)]) == 1
    assert not env_file.exists()
    assert "security_state" in capsys.readouterr().err


def test_a_pin_that_is_not_a_pin_or_names_a_missing_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    bad = tmp_path / "pin.json"
    for text in ("not json", "{}", json.dumps({"file": "x", "sha256": "xyz"}), json.dumps({"sha256": "0" * 64})):
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(lab.Refused):
            lab.load_pin(bad)
        assert lab.main(["set", "--pin", str(bad)]) == 1
    bad.write_text(json.dumps({"file": str(tmp_path / "nowhere.json"), "sha256": "0" * 64}), encoding="utf-8")
    assert lab.main(["set", "--pin", str(bad)]) == 1


def test_the_command_runs_as_the_workflows_run_it():
    done = subprocess.run([sys.executable] + RUN.split()[1:], cwd=ROOT, capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "GITHUB_ENV"})
    assert done.returncode == 0, done.stderr
    assert done.stdout == "STEALTHFOX_SECURITY_STATE=%s\n" % RESULT.parent.resolve()

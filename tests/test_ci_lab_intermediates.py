"""scripts/ci_lab_intermediates.py: a CI leg launches with a fixed list of intermediates that the repository holds, and the core is told so."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("ci_lab_intermediates_under_test", ROOT / "scripts" / "ci_lab_intermediates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lab = _load()
PIN = ROOT / "ci" / "intermediates.pin.json"
LIST = ROOT / "ci" / "intermediates.json"
RUN = "python scripts/ci_lab_intermediates.py set --pin ci/intermediates.pin.json"


def test_the_pin_is_the_hash_of_the_file_it_names():
    pin = lab.load_pin(PIN)
    assert pin["file"] == "ci/intermediates.json" and pin["comment"]
    assert hashlib.sha256(LIST.read_bytes()).hexdigest() == pin["sha256"]
    assert len(LIST.read_bytes()) == pin["bytes"]


def test_the_file_is_a_valid_list_of_format_1_with_no_certificate():
    raw = LIST.read_bytes()
    listing = json.loads(raw)
    assert listing["format"] == 1 and listing["collection"] == "security-state/intermediates"
    assert listing["count"] == len(listing["certs"]) == 0
    assert listing["set_sha256"] == hashlib.sha256(b"").hexdigest()      # the hash of no derHash at all
    assert list(listing) == ["format", "collection", "collection_timestamp", "fetched_utc", "count", "set_sha256", "certs"]
    assert raw == json.dumps(listing, separators=(",", ":"), ensure_ascii=True).encode("utf-8"), "not written as the core writes it (compact, no final newline)"


def test_set_writes_the_variable_with_the_absolute_path_to_github_env(tmp_path, monkeypatch):
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    assert lab.main(["set", "--pin", str(PIN)]) == 0
    assert env_file.read_text(encoding="utf-8") == "STEALTHFOX_INTERMEDIATES=%s\n" % LIST.resolve()
    assert Path(LIST.resolve()).is_absolute()


def test_without_github_env_it_prints_the_line(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    assert lab.main(["set", "--pin", str(PIN)]) == 0
    assert capsys.readouterr().out == "STEALTHFOX_INTERMEDIATES=%s\n" % LIST.resolve()


def test_a_file_that_is_not_the_pinned_one_is_refused_and_exports_nothing(tmp_path, monkeypatch, capsys):
    other = tmp_path / "other.json"
    other.write_bytes(LIST.read_bytes() + b"\n")
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"file": str(other), "sha256": lab.load_pin(PIN)["sha256"]}), encoding="utf-8")
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    assert lab.main(["set", "--pin", str(pin)]) == 1
    assert not env_file.exists()
    assert "change both in one commit" in capsys.readouterr().err


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
    import subprocess
    import sys

    done = subprocess.run([sys.executable] + RUN.split()[1:], cwd=ROOT, capture_output=True, text=True, env={k: v for k, v in __import__("os").environ.items() if k != "GITHUB_ENV"})
    assert done.returncode == 0, done.stderr
    assert done.stdout == "STEALTHFOX_INTERMEDIATES=%s\n" % LIST.resolve()

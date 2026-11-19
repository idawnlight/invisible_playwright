"""A unit test does not depend on the environment of whoever runs it: the seal of the job is not its seal.

The CI jobs export ``INVISIBLE_SEAL_FILE`` (a LOCAL seal of the candidate, with no published assets) for the e2e tests that launch the engine under test.
Ten unit tests that compose a launch (``build_env``, ``launch_persistent_context``, a second browser served by a virtual display, the session token of the
async API) reached ``ensure_binary`` with it and died with ``SealError: the active seal ... is a LOCAL seal with no published assets``, in the candidate
and in no other place. ``tests/conftest.py`` takes the variable out of the environment of every test that is not e2e (and drops the seal the process had
already resolved from it).

Known-bad input: a local seal in ``INVISIBLE_SEAL_FILE``, as the job has it. On the conftest without the fixture the second test fails; the first one is the
same assertion in the environment of the job itself, where the variable is always there.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
LOCAL_SEAL = {"schema": 2, "tag": "firefox-65-candidate", "upstream_version": "151.0", "build_id": "20260724001949",
              "source_commit": "", "playwright": {"min": "1.55.0", "max": "1.61.0"}, "assets": {}}


def test_a_unit_test_sees_the_packaged_seal_and_not_a_local_one():
    from invisible_core.seal import active_seal

    assert "INVISIBLE_SEAL_FILE" not in os.environ
    assert not active_seal().is_local


def test_a_unit_test_run_with_a_local_seal_in_the_environment_does_not_see_it(tmp_path):
    seal = tmp_path / "gate.seal.json"
    seal.write_text(json.dumps(LOCAL_SEAL, sort_keys=True), encoding="utf-8")
    env = dict(os.environ, INVISIBLE_SEAL_FILE=str(seal))
    node = "tests/test_a_unit_test_does_not_read_the_seal_of_the_job.py::test_a_unit_test_sees_the_packaged_seal_and_not_a_local_one"
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", node],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-500:]

"""Execute the fork's README install block and first-launch example.

The harness imports only stdlib and pytest. Both Python packages are installed
from the two Git references written in README.md, into a new non-editable venv.
Browser downloads use a separate cache, with no local seal or executable passed
by the caller. Run explicitly with ``-m e2e -o addopts=`` after publishing the
README's Git references and engine release.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_LEAKS = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
          "INVISIBLE_PLAYWRIGHT_ENGINE_DIR", "INVISIBLE_FIREFOX_RELEASE_REPOSITORY",
          "INVISIBLE_PLAYWRIGHT_CACHE_DIR",
          "INVISIBLE_DOWNLOAD_DEADLINE", "STEALTHFOX_GITHUB_TOKEN", "GITHUB_TOKEN",
          "INVISIBLE_SEAL_FILE", "INVISIBLE_CORE_PIN", "INVISIBLE_CORE_AUTOFIX",
          "INVISIBLE_CORE_AUTOFIX_TIMEOUT", "UV", "STEALTHFOX_GEOIP_MMDB",
          "STEALTHFOX_FONT_MANIFEST", "STEALTHFOX_WEBRTC_PUBLIC_IP",
          "INVPW_CURSOR_TIMER", "INVISIBLE_CORE_AUTOFIX_ATTEMPTED",
          "INVPW_SESSION_TOKEN", "INVPW_DESKTOP", "STEALTHFOX_WEBRTC_DISABLE_IPV6")


def _clean_env(base=None):
    env = dict(os.environ if base is None else base)
    removed = [key for key in _LEAKS if key in env]
    for key in removed:
        env.pop(key, None)
    return env, removed


def _run(cmd, *, timeout: int = 600, check: bool = True, env=None, cwd=None):
    env, removed = _clean_env(env)
    result = subprocess.run([str(part) for part in cmd], capture_output=True,
                            text=True, timeout=timeout, env=env,
                            cwd=str(cwd) if cwd is not None else None)
    if check and result.returncode != 0:
        raise AssertionError(
            f"{cmd!r} exited {result.returncode}\n--- stdout ---\n"
            f"{result.stdout[-3000:]}\n--- stderr ---\n{result.stderr[-3000:]}"
            f"\nremoved inherited overrides: {removed}")
    return result


def _venv_python(venv_dir):
    bindir, name = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return Path(venv_dir) / bindir / name


run_checked = _run
venv_python = _venv_python
README = Path(__file__).resolve().parents[1] / "README.md"
_VCS_REQUIREMENT = re.compile(
    r"(invisible[-_](?:core|playwright))\s*@\s*git\+"
    r"(https://github\.com/[^\s\"']+\.git)@([^\s\"']+)")


def readme_install_block(text=None) -> list[str]:
    """Select the complete shell block containing both pip Git requirements."""
    text = README.read_text(encoding="utf-8") if text is None else text
    candidates = []
    for body in re.findall(r"```(?:bash|sh)\n(.*?)```", text, re.S):
        names = {name.replace("_", "-") for name, _, _ in _VCS_REQUIREMENT.findall(body)}
        if names == {"invisible-core", "invisible-playwright"}:
            assert re.search(r"python\s+-m\s+pip\s+install\b", body), body
            assert re.search(r"python3?\s+-m\s+venv\s+\.venv\b", body), body
            candidates.append([line for line in body.splitlines() if line.strip()])
    assert len(candidates) == 1, (
        "README needs exactly one runnable venv/pip block installing both Git references")
    return candidates[0]


def readme_git_requirements(lines=None) -> dict:
    lines = readme_install_block() if lines is None else lines
    found = _VCS_REQUIREMENT.findall("\n".join(lines))
    requirements = {name.replace("_", "-"): {"url": url, "ref": ref}
                    for name, url, ref in found}
    assert len(found) == 2 and set(requirements) == {"invisible-core", "invisible-playwright"}
    return requirements


def readme_browser_example() -> str:
    examples = re.findall(r"```python\n(.*?)```", README.read_text(encoding="utf-8"), re.S)
    assert examples, "README has no Python first-launch example"
    example = examples[0]
    assert "InvisiblePlaywright" in example
    assert "binary_path" not in example, "the first-launch example must select the default release"
    return example


def readme_engine_release() -> tuple[str, str]:
    matches = re.findall(r"https://github\.com/([^\s/)]+/[^\s/)]+)/releases/tag/([^\s)]+)",
                         README.read_text(encoding="utf-8"))
    assert len(set(matches)) == 1, "README must identify one default engine release"
    return matches[0]


def run_readme_block(lines, *, cwd: Path, env: dict):
    """Execute the documented Linux shell commands without changing their text."""
    env = dict(env, PIP_NO_CACHE_DIR="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PYTHONNOUSERSITE="1")
    script = cwd / "install.sh"
    script.write_text("set -eo pipefail\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return _run(["bash", script], cwd=cwd, env=env, timeout=1800)


def _installed(py: Path) -> dict:
    script = """
import importlib, importlib.metadata, json, platform
out = {"python": platform.python_version(), "packages": {}}
for name in ["invisible_core", "invisible_playwright"]:
    module = importlib.import_module(name)
    dist = importlib.metadata.distribution(name)
    out["packages"][name.replace("_", "-")] = {
        "version": dist.version, "path": module.__file__,
        "direct_url": json.loads(dist.read_text("direct_url.json")),
        "requires": dist.requires, "python": dist.metadata.get("Requires-Python"),
    }
print(json.dumps(out))
"""
    return json.loads(_run([py, "-I", "-c", script], timeout=60).stdout.splitlines()[-1])


def _installed_seal(py: Path) -> dict:
    script = ("import invisible_core, json; from pathlib import Path; "
              "print(Path(invisible_core.__file__).with_name('seal.json').read_text())")
    return json.loads(_run([py, "-I", "-c", script], timeout=60).stdout)


def _upstream_version(py: Path) -> str:
    return _installed_seal(py)["upstream_version"]


def _remote_commit(url: str, ref: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    result = _run(["git", "ls-remote", url,
                   f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}"],
                  timeout=90)
    refs = dict(line.split()[::-1] for line in result.stdout.splitlines())
    commit = (refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}")
              or refs.get(f"refs/heads/{ref}"))
    assert commit, f"README reference {ref!r} does not exist in {url}"
    return commit


@pytest.fixture(scope="module")
def workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="invpw-install-e2e-"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def isolated_cache_env(workspace: Path) -> dict:
    # The README currently ships a Linux engine. XDG_CACHE_HOME isolates both
    # the browser and state while inherited product overrides are removed.
    return dict(os.environ, XDG_CACHE_HOME=str(workspace / "cache"), PYTHONNOUSERSITE="1")


@pytest.fixture(scope="module")
def clean_venv(workspace: Path, isolated_cache_env: dict) -> Path:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        pytest.skip("the README currently provides a Linux installation and engine")
    run_readme_block(readme_install_block(), cwd=workspace, env=isolated_cache_env)
    python = _venv_python(workspace / ".venv")
    assert python.exists(), "README install block did not create its documented .venv"
    return python


@pytest.mark.unit
def test_readme_install_block_selects_both_git_sources():
    requirements = readme_git_requirements()
    assert all(value["ref"] for value in requirements.values())
    assert all(value["url"].startswith("https://github.com/")
               for value in requirements.values())


@pytest.mark.unit
def test_readme_install_block_rejects_an_index_only_install():
    text = "```bash\npython3 -m venv .venv\npython -m pip install invisible-playwright\n```"
    with pytest.raises(AssertionError, match="both Git references"):
        readme_install_block(text)


@pytest.mark.e2e
def test_clean_install_the_way_a_user_does_it(clean_venv: Path, workspace: Path):
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    installed = _installed(clean_venv)
    for name, expected in readme_git_requirements().items():
        package = installed["packages"][name]
        assert Path(package["path"]).is_relative_to(workspace / ".venv"), package
        direct = package["direct_url"]
        assert direct["url"] == expected["url"], direct
        assert direct["vcs_info"]["requested_revision"] == expected["ref"], direct
        assert direct["vcs_info"]["commit_id"] == _remote_commit(**expected), direct
        assert installed["python"] in SpecifierSet(package["python"])
    wrapper = installed["packages"]["invisible-playwright"]
    core = installed["packages"]["invisible-core"]
    pin = next(Requirement(value) for value in wrapper["requires"]
               if Requirement(value).name.replace("_", "-") == "invisible-core")
    assert str(pin.specifier) == f"=={core['version']}", (pin, core)
    _run([clean_venv, "-m", "pip", "check"], timeout=120)


@pytest.mark.e2e
def test_the_resolved_playwright_is_inside_the_range_the_package_declares(clean_venv: Path):
    script = ("from invisible_playwright._pw._repo_version import version; "
              "from invisible_core.seal import active_seal; s = active_seal(); "
              "print(version); print(s.playwright_min); print(s.playwright_max)")
    vendored, low, high = _run([clean_venv, "-I", "-c", script]).stdout.strip().splitlines()[-3:]
    from packaging.version import Version
    assert Version(low) <= Version(vendored) <= Version(high), (vendored, low, high)


@pytest.mark.e2e
def test_version_command_reports_wrapper_and_binary(clean_venv: Path):
    result = _run([clean_venv, "-m", "invisible_playwright", "version"], timeout=30)
    assert readme_engine_release()[1] in result.stdout, result.stdout
    assert _upstream_version(clean_venv) in result.stdout, result.stdout


@pytest.mark.e2e
def test_fetch_against_live_release(clean_venv: Path, isolated_cache_env: dict):
    result = _run([clean_venv, "-m", "invisible_playwright", "fetch"],
                  env=isolated_cache_env, timeout=1800)
    binary = Path(result.stdout.strip().splitlines()[-1])
    assert binary.is_file(), result.stdout
    assert "SHA256 mismatch" not in result.stderr


@pytest.mark.e2e
def test_binary_executes_after_fetch(clean_venv: Path, isolated_cache_env: dict):
    result = _run([clean_venv, "-I", "-c",
                   "from invisible_core import ensure_binary; print(ensure_binary())"],
                  env=isolated_cache_env, timeout=1800)
    binary = Path(result.stdout.strip().splitlines()[-1])
    result = _run([binary, "--version"], env=isolated_cache_env, timeout=30)
    assert result.returncode == 0
    assert _upstream_version(clean_venv) in result.stdout + result.stderr


@pytest.mark.e2e
def test_playwright_launch_against_real_site(clean_venv: Path, workspace: Path,
                                             isolated_cache_env: dict):
    # Execute the README example with a new cache, even if fetch ran earlier.
    cache = workspace / "first-launch-cache"
    assert not cache.exists()
    env = dict(isolated_cache_env, XDG_CACHE_HOME=str(cache))
    script = workspace / "readme-smoke.py"
    script.write_text(readme_browser_example(), encoding="utf-8")
    result = _run([clean_venv, "-I", script], env=env, timeout=1800)
    assert "Example Domain" in result.stdout, result.stdout
    assert cache.is_dir(), "first launch did not create its isolated cache"
    cached = _run([clean_venv, "-I", "-c",
                   "from invisible_core import ensure_binary; print(ensure_binary())"],
                  env=env, timeout=60)
    binary = Path(cached.stdout.strip().splitlines()[-1])
    assert binary.is_file() and binary.is_relative_to(cache), binary


@pytest.mark.e2e
def test_e2e_marker_is_excluded_by_default():
    assert True


@pytest.mark.e2e
def test_the_published_version_has_a_github_release(clean_venv: Path):
    """The engine release named by the README serves every sealed asset."""
    import urllib.parse
    import urllib.request

    repository, tag = readme_engine_release()
    seal = _installed_seal(clean_venv)
    assert seal["tag"] == tag, (seal["tag"], tag)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        release = json.load(response)
    assert release["draft"] is False and release["tag_name"] == tag
    assets = {asset["name"]: asset for asset in release["assets"]}
    assert seal["assets"], "the installed core has no downloadable engine assets"
    for name, expected in seal["assets"].items():
        assert name in assets, f"{name} is absent from {repository} release {tag}"
        assert assets[name]["size"] == expected["size"], (name, assets[name], expected)
        digest = assets[name].get("digest")
        if digest:
            assert digest == f"sha256:{expected['sha256']}", (name, digest)


@pytest.mark.e2e
def test_the_changelog_documents_every_version_it_claims_to_cover(clean_venv: Path):
    """The installed Git-tag version has a dated entry in this checkout."""
    version = _installed(clean_venv)["packages"]["invisible-playwright"]["version"]
    changelog = (README.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"(?m)^##\s*\[{re.escape(version)}\]\s*-\s*\d{{4}}-\d{{2}}-\d{{2}}\s*$",
                     changelog), f"CHANGELOG.md has no dated entry for {version}"

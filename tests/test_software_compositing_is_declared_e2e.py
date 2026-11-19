"""The engine DECLARES software compositing on every machine, and what a page reads does not change by it.

The core declares, in the feature-status table (`zoom.stealth.gfx.feature_status`), the decision that Firefox retail takes on a
virtual machine: the compositing features (WEBRENDER, WEBRENDER_COMPOSITOR, the D3D*_LAYERS, CANVAS2D_ACCELERATION, ...) are
blocklisted with FEATURE_FAILURE_VM_VENDOR, and on Windows the core sets `gfx.webrender.software=true` and
`gfx.webrender.software.d3d11=false`. WebGL, WebGPU and the media features stay as the persona declares them. Three things follow, one test each:

1. the compositor the engine runs is WebRender (Software), on Windows AND on Linux (on Linux this closes the risk that the table,
   with no pref beside it, sends the engine down another road);
2. the GPU process, which is where a hardware WebRender spends its CPU, spends almost none on a fixed scene;
3. no byte a page reads of canvas 2D, WebGL, WebGPU, text and the declared values changes: the keys of the probe (the 86 of the first
   hunt, WebGPU features and limits read by name, and the canvas 2D scenes added for the canvas blocklist: drawImage scaled up and down
   with imageSmoothing off and on at three qualities, text filled and stroked with gradients of three kinds) equal the reference of the
   operating system, tests/data/page_bytes_<os>.json.

Known-bad input: the engine with the core of firefox-64 before this declaration (64.71.0). On the Windows runner `gfx.webrender.software`
is false there (the effective prefs of run 43652292533, arm ours) and the GPU process uses 2.7 to 4.2 s on the scene of test 2 (GitHub
Actions runs 43652292533 and 43642744101): tests 1 and 2 are red on it. Whether test 1 is red on Linux with that core is not measured.

How the compositor is read. `Troubleshoot.snapshot().graphics` is what about:support shows and carries `windowLayerManagerType`, but on
this engine the provider throws before it gets there (`ext is null`: it asks WebGL for the chrome-only MOZ_debug extension, which the
engine does not give), so `graphics` is an error string. The test reads what the provider reads, the same two properties of each window
(`windowUtils.layerManagerType` and `.layerManagerRemote`, Troubleshoot.sys.mjs), in chrome scope through Marionette: the route
tests/webfont_probe.py uses (`-marionette -remote-allow-system-access` and MOZ_REMOTE_ALLOW_SYSTEM_ACCESS=1).
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

OS_NAME = "windows" if sys.platform == "win32" else "linux"
SOFTWARE = "WebRender (Software)"
MARIONETTE_ARGS = ["-marionette", "-remote-allow-system-access"]

#: What a launch is given in the hunt that measured the Windows reference (same seed, locale and timezone, so the same persona).
LAUNCH = dict(seed=42, headless=True, locale="en-US", timezone="Europe/Rome")

# --------------------------------------------------------------------------------------------------------- the privileged reads

WINDOWS_JS = """
const out = [];
for (const win of Services.ww.getWindowEnumerator()) {
  try {
    const u = win.windowUtils;
    out.push([u.layerManagerType, u.layerManagerRemote]);
  } catch (e) { out.push(["error: " + e, false]); }
}
return JSON.stringify(out);
"""

PREFS_JS = """
const out = {};
for (const name of arguments[0]) {
  const t = Services.prefs.getPrefType(name);
  out[name] = t === 32 ? Services.prefs.getStringPref(name)
            : t === 64 ? Services.prefs.getIntPref(name)
            : t === 128 ? Services.prefs.getBoolPref(name) : null;
}
return JSON.stringify(out);
"""


def _marionette(port):
    import webfont_probe

    m = webfont_probe._Marionette(port)
    m.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
    m.call("Marionette:SetContext", {"value": "chrome"})
    return m


def _script(marionette, script, args=()):
    return json.loads(marionette.call("WebDriver:ExecuteScript", {"script": script, "args": list(args)})["value"])


def _a_page_server(page_html):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = page_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/" % server.server_address[1]


# ------------------------------------------------------------------------------------------------ 1. the compositor

def test_the_compositor_is_webrender_software_whatever_the_machine(firefox_binary, monkeypatch):
    import webfont_probe
    from invisible_playwright import InvisiblePlaywright

    monkeypatch.setenv("MOZ_REMOTE_ALLOW_SYSTEM_ACCESS", "1")   # the chrome-scope reads below
    port = webfont_probe.marionette_port()
    pref_names = ["zoom.stealth.gfx.feature_status", "gfx.webrender.software", "gfx.webrender.software.d3d11"]
    server, url = _a_page_server("<!doctype html><title>compositor</title><body>x</body>")
    marionette = None
    try:
        with InvisiblePlaywright(binary_path=firefox_binary, extra_args=MARIONETTE_ARGS,
                                 extra_prefs={"marionette.port": port}, **LAUNCH) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="load", timeout=60000)
            marionette = _marionette(port)
            # a window gets its layer manager a moment after it is shown: the same wait for the answer as about:support has none, so poll
            windows, end = [], time.time() + 30
            while time.time() < end:
                windows = _script(marionette, WINDOWS_JS)
                if any(kind not in ("None", "") and remote for kind, remote in windows):
                    break
                time.sleep(0.5)
            prefs = _script(marionette, PREFS_JS, [pref_names])
    finally:
        if marionette is not None:
            marionette.close()
        server.shutdown()

    shown = [kind for kind, remote in windows if kind != "None" and remote]
    assert shown, "no window of the engine has a layer manager (windowUtils.layerManagerType, layerManagerRemote): %r" % (windows,)
    assert set(shown) == {SOFTWARE}, (
        "the compositor of the window(s) is %r on %s, expected %r: the engine did not take the VM decision for the compositing features"
        % (shown, OS_NAME, SOFTWARE))

    table = {}
    for entry in (prefs["zoom.stealth.gfx.feature_status"] or "").split(","):
        name, _, rest = entry.partition("|")
        table[name] = rest.split("|")
    webrender = table.get("WEBRENDER")
    assert webrender is not None, "the declared table has no WEBRENDER entry: %r" % (prefs["zoom.stealth.gfx.feature_status"],)
    assert webrender[1:2] == ["FEATURE_FAILURE_VM_VENDOR"], (
        "WEBRENDER is declared %r, expected blocklisted with FEATURE_FAILURE_VM_VENDOR" % (webrender,))
    if OS_NAME == "windows":
        assert prefs["gfx.webrender.software"] is True, "gfx.webrender.software is %r on Windows" % (prefs["gfx.webrender.software"],)
        assert prefs["gfx.webrender.software.d3d11"] is False, \
            "gfx.webrender.software.d3d11 is %r on Windows" % (prefs["gfx.webrender.software.d3d11"],)


# ------------------------------------------------------------------------------------------------ 2. the GPU process

#: The scene of the hunt that measured it (scripts/hunt_cpu_per_browser.py of the branch hunt/cpu-render-path): a page that builds an
#: RTCPeerConnection with no ICE servers and waits for the gather, draws 200 DOM elements and one 2D canvas frame; the browser goes to
#: the next copy of it, 10 times.
ROUNDS = 10
ICE_WAIT_MS = 30000
SCENE = """<!doctype html><meta charset=utf-8><title>scenario</title>
<body><div id=root></div><canvas id=c width=320 height=200></canvas>
<script>
window.__ready = (async () => {
  const pc = new RTCPeerConnection({iceServers: []});
  pc.createDataChannel("x");
  const done = new Promise(res => {
    const t = setTimeout(() => res("timeout:" + pc.iceGatheringState), %(ice_wait)d);
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") { clearTimeout(t); res("complete"); }
    });
  });
  await pc.setLocalDescription(await pc.createOffer());
  const state = pc.iceGatheringState === "complete" ? "complete" : await done;
  const root = document.getElementById("root");
  for (let i = 0; i < 200; i++) {
    const d = document.createElement("div");
    d.textContent = "element " + i;
    d.style.cssText = "display:inline-block;width:40px;height:16px;margin:1px;background:hsl(" + (i * 7 %% 360) + ",60%%,60%%)";
    root.appendChild(d);
  }
  const g = document.getElementById("c").getContext("2d");
  for (let i = 0; i < 40; i++) {
    g.fillStyle = "hsl(" + (i * 9) + ",70%%,50%%)";
    g.fillRect(i * 8, (i * 5) %% 160, 60, 30);
  }
  g.font = "16px sans-serif";
  g.fillText("a frame of the scenario", 10, 100);
  g.getImageData(0, 0, 1, 1);
  pc.close();
  return state;
})();
</script>
""" % {"ice_wait": ICE_WAIT_MS}

#: CPU seconds the GPU process may use for the 10 scenes of ONE browser, per operating system.
#:
#: WINDOWS: 1.5 s. Measured on the windows-2022 runner (Hyper-V, no real GPU), 4 browsers at once, the same 10 scenes each, GPU process,
#: median per browser: 0.32 s with the VM decision declared (run 43652292533, arm ours_vmretail, mean of two rounds 0.324 s) and 2.7 to
#: 4.2 s without it (run 43652292533, arm ours, 2.68 s, and run 43642744101). Stock Firefox 159 headed on the same runner: 0.004 s.
#: 1.5 s is about 4.7 times what the declared engine used and 0.55 times the least the undeclared one used: a run that is slow for
#: the runner's sake stays under it, and the hardware WebRender, which keeps the GPU process busy for every frame, does not.
#: This test starts ONE browser, which the GPU process has to itself, so it is at most what a browser used when four shared the runner.
#: It is CPU time (user + kernel of the process, `_cpu_by_type`), not wall-clock: a runner that is slow stretches the clock, not the CPU.
#:
#: LINUX: THERE IS NO GPU PROCESS. Software WebRender composites in the parent, and the engine's tree on Linux is the parent, the
#: forkserver, the content processes (`tab`), the socket, the RDD and utility processes (WSL, firefox-64, /proc read; e2e Linux run
#: 43701692047 saw the same). The test says so and asserts that fact: no GPU process. The CPU of the parent is printed and judged by
#: nothing (20 runs on two runners, run 43707494271: 2.11 to 2.46 s, and a CPU time depends on the machine and on what else runs).
GPU_CPU_LIMIT_S = {"windows": 1.5}


def test_the_gpu_process_is_idle_on_a_fixed_scene(firefox_binary, capsys):
    import os

    from _cpu_by_type import CpuSampler, engine_processes
    from _waits import watch
    from invisible_playwright import InvisiblePlaywright

    server, base = _a_page_server(SCENE)
    states = []
    try:
        with InvisiblePlaywright(binary_path=firefox_binary, **LAUNCH) as browser:
            page = browser.new_page()
            # The sampling begins when the engine has a content process: one that is born after the start is counted from
            # zero, but a tree that is seen before it has one gave "seen: ['parent']" twice. Waited for with a deadline, and
            # what the tree held at each look is in the message if it never comes.
            tree = watch(lambda: tuple(sorted({kind for kind, _cpu in engine_processes(os.getpid()).values()})),
                         lambda kinds: "parent" in kinds and "tab" in kinds, 30, lambda: page.wait_for_timeout(100))
            assert tree.ok, "no content process (`tab`) of the engine below the test process within 30 s; types seen: %s" % tree.render()
            sampler = CpuSampler(os.getpid())
            sampler.begin()
            try:
                for n in range(ROUNDS):
                    page.goto("%sp/%d" % (base, n), wait_until="load", timeout=60000)
                    states.append(page.evaluate("window.__ready"))
            finally:
                cpu = sampler.end()
            seen = sorted({kind for kind, _cpu in sampler.last.values()})
    finally:
        server.shutdown()

    with capsys.disabled():
        print("\nCPU seconds of the engine by process type over %d scenes of one browser on %s: %s (types seen: %s)"
              % (ROUNDS, OS_NAME, json.dumps({k: round(v, 3) for k, v in cpu.items() if v or k in seen}), ", ".join(seen)))
    assert states == ["complete"] * ROUNDS, "the scene did not run %d times to its end: %r" % (ROUNDS, states)
    assert "parent" in seen and "tab" in seen, \
        "no parent and content process of the engine below the test process (seen: %r): the process tree is cut and the CPU is not the browser's" % (seen,)
    if OS_NAME == "linux":
        assert "gpu" not in seen, "the Linux engine has a GPU process (seen: %r): software WebRender was measured without one" % (seen,)
        return
    limit = GPU_CPU_LIMIT_S[OS_NAME]
    assert "gpu" in seen, "the Windows engine has no GPU process (seen: %r); the measurement of the bound had one" % (seen,)
    if "gpu" in seen:
        assert cpu["gpu"] < limit, (
            "the GPU process used %.2f s of CPU for %d scenes of one browser on %s, the bound is %.1f s (all types: %s). With the compositing "
            "declared as software it used 0.32 s on the Windows runner (run 43652292533), without it 2.7 to 4.2 s (runs 43652292533, 43642744101)"
            % (cpu["gpu"], ROUNDS, OS_NAME, limit, json.dumps({k: round(v, 2) for k, v in cpu.items() if v})))


# ------------------------------------------------------------------------------------------------ 3. what a page reads

def test_the_bytes_a_page_reads_are_the_reference_of_the_system(firefox_binary):
    import _page_bytes
    from invisible_playwright import InvisiblePlaywright

    expression = (Path(__file__).resolve().parent / "page_bytes_probe.js").read_text(encoding="utf-8")
    server, url = _a_page_server("<!doctype html><meta charset=utf-8><title>probe</title><body></body>")
    try:
        with InvisiblePlaywright(binary_path=firefox_binary, **LAUNCH) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="load", timeout=60000)
            measured = page.evaluate(expression)
    finally:
        server.shutdown()

    problems, record = _page_bytes.compare(_page_bytes.load_reference(OS_NAME), measured)
    assert not problems, _page_bytes.message(OS_NAME, problems, record)

"""A WebGL canvas has the same pixels through synchronous and video readback.

captureStream reaches Firefox's asynchronous WebGL snapshot path. Exercise its
alpha representation with the seed's projection enabled, using actual video
frames rather than a stand-in for the readback implementation.
"""
from __future__ import annotations

import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e

SCRIPT = r"""
window.readbackCase = async function({contextName, alpha, premultipliedAlpha, pattern}) {
  const width = 24, height = 18;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  document.body.appendChild(canvas);
  const gl = canvas.getContext(contextName, {
    alpha, premultipliedAlpha, preserveDrawingBuffer: true, antialias: false,
  });
  if (!gl) return {unsupported: true, contextName};
  const result = {attributes: gl.getContextAttributes(), states: [], frames: []};
  const matrix = [
    [1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1], [1, 1, 1, 1],
    [1, 0, 0, .5], [0, 1, 0, .5], [0, 0, 1, .5], [1, 1, 0, .5],
    [1, 0, 1, .25], [0, 1, 1, .25], [0, 0, 0, 0], [.5, .25, .75, .75],
  ];
  function draw() {
    gl.enable(gl.SCISSOR_TEST);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        let color = matrix[Math.floor(y / 6) * 4 + Math.floor(x / 6)].slice();
        if (pattern === 'varying') {
          color = [(x * 37 + y * 11) % 251 / 255,
                   (x * 17 + y * 43) % 253 / 255,
                   (x * 29 + y * 7) % 249 / 255, ((x + y) % 3 + 1) / 4];
        }
        if (premultipliedAlpha) {
          for (let k = 0; k < 3; k++) color[k] *= color[3];
        }
        gl.scissor(x, y, 1, 1);
        gl.clearColor(...color);
        gl.clear(gl.COLOR_BUFFER_BIT);
      }
    }
    gl.disable(gl.SCISSOR_TEST);
    gl.finish();
  }
  function pixels(source) {
    const target = document.createElement('canvas');
    target.width = width;
    target.height = height;
    const ctx = target.getContext('2d', {willReadFrequently: true});
    ctx.drawImage(source, 0, 0, width, height);
    return Array.from(ctx.getImageData(0, 0, width, height).data);
  }
  async function bounded(promise, operation) {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(operation + ' did not complete')), 20000);
      })]);
    } finally {
      clearTimeout(timer);
    }
  }
  draw();
  const direct = new Uint8Array(width * height * 4);
  gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, direct);
  result.readPixels = Array.from(direct);
  result.readPixelsError = gl.getError();
  if (contextName === 'webgl2') {
    const pack = gl.createBuffer();
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, pack);
    gl.bufferData(gl.PIXEL_PACK_BUFFER, direct.byteLength, gl.STREAM_READ);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, 0);
    const packed = new Uint8Array(direct.length);
    gl.getBufferSubData(gl.PIXEL_PACK_BUFFER, 0, packed);
    result.pbo = Array.from(packed);
    result.pboError = gl.getError();
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
    gl.deleteBuffer(pack);
  }
  const sentinelFB = gl.createFramebuffer();
  gl.bindFramebuffer(gl.FRAMEBUFFER, sentinelFB);
  const sentinelPBO = contextName === 'webgl2' ? gl.createBuffer() : null;
  if (sentinelPBO) {
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, sentinelPBO);
    gl.bufferData(gl.PIXEL_PACK_BUFFER, 16, gl.STREAM_READ);
  }
  function state(phase) {
    result.states.push({phase,
      framebuffer: gl.getParameter(gl.FRAMEBUFFER_BINDING) === sentinelFB,
      readFramebuffer: contextName === 'webgl2'
          ? gl.getParameter(gl.READ_FRAMEBUFFER_BINDING) === sentinelFB : null,
      pbo: sentinelPBO ? gl.getParameter(gl.PIXEL_PACK_BUFFER_BINDING) === sentinelPBO : null,
      error: gl.getError(),
    });
  }
  let stream, video, redrawTimer;
  try {
    state('before');
    result.drawImage = pixels(canvas);
    state('after-drawImage');
    stream = canvas.captureStream(30);
    video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    document.body.appendChild(video);
    const redraw = () => {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      draw();
      gl.bindFramebuffer(gl.FRAMEBUFFER, sentinelFB);
    };
    // These Firefox versions do not expose track.requestFrame. Keep the
    // canvas changing so captureStream delivers new frames to the video.
    redrawTimer = setInterval(redraw, 30);
    const playing = video.play();
    redraw();
    await bounded(playing, 'video.play');
    result.video = {width: video.videoWidth, height: video.videoHeight};
    result.frames.push(pixels(video));
    state('initial-video-frame');
    for (let n = 0; n < 3; n++) {
      const presented = new Promise(resolve => video.requestVideoFrameCallback(resolve));
      redraw();
      await bounded(presented, 'video frame');
      result.frames.push(pixels(video));
      state('video-frame-' + n);
    }
    state('after-capture');
    return result;
  } finally {
    clearInterval(redrawTimer);
    if (stream) stream.getTracks().forEach(track => track.stop());
    if (video) video.remove();
    if (sentinelPBO) {
      gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
      gl.deleteBuffer(sentinelPBO);
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.deleteFramebuffer(sentinelFB);
    canvas.remove();
    gl.getExtension('WEBGL_lose_context')?.loseContext();
  }
};
"""


@pytest.fixture(scope="module")
def readback_site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/readback.js":
                body, content_type = SCRIPT.encode(), "text/javascript"
            else:
                body = (b"<!doctype html><meta charset=utf-8><title>Async canvas readback</title>"
                        b"<body><script src=/readback.js></script>")
                content_type = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/" % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(scope="module")
def readback_browser(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    # Xvfb/hidden-desktop headed rendering: native Firefox --headless does
    # not provide WebGL on every host and would miss the path this exercises.
    with InvisiblePlaywright(
        seed=42, binary_path=firefox_binary, headless=True, humanize=False,
        locale="en-US", timezone="Etc/UTC",
        extra_prefs={"media.autoplay.default": 0},
    ) as browser:
        yield browser


def _pixel_difference(expected, actual):
    differences = [(n, a, b) for n, (a, b) in enumerate(zip(expected, actual)) if a != b]
    return {
        "expected_bytes": len(expected), "actual_bytes": len(actual),
        "changed_channels": len(differences),
        "maximum_delta": max((abs(a - b) for _, a, b in differences), default=0),
        "examples": differences[:8],
    }


@pytest.mark.parametrize("context_name", ["webgl", "webgl2"])
@pytest.mark.parametrize("alpha", [True, False], ids=["alpha", "opaque"])
@pytest.mark.parametrize("premultiplied_alpha", [True, False], ids=["premultiplied", "straight"])
def test_video_frames_match_canvas_draw_image_with_the_same_alpha_representation(
        readback_browser, readback_site, context_name, alpha, premultiplied_alpha):
    page = readback_browser.new_page()
    try:
        page.goto(readback_site)
        results = []
        for pattern in ("matrix", "varying"):
            result = page.evaluate("options => readbackCase(options)", {
                "contextName": context_name, "alpha": alpha,
                "premultipliedAlpha": premultiplied_alpha, "pattern": pattern,
            })
            results.append((pattern, result))
        mismatches = []
        for pattern, result in results:
            assert not result.get("unsupported"), (context_name, pattern)
            assert result["attributes"]["alpha"] is alpha
            assert result["attributes"]["premultipliedAlpha"] is premultiplied_alpha
            assert result["video"] == {"width": 24, "height": 18}
            assert result["readPixelsError"] == 0
            if context_name == "webgl2":
                assert result["pboError"] == 0
                assert result["pbo"] == result["readPixels"]
            assert len(result["states"]) == 7
            for state in result["states"]:
                assert state["framebuffer"] is True, (pattern, state)
                assert state["readFramebuffer"] is not False, (pattern, state)
                assert state["pbo"] is not False, (pattern, state)
                assert state["error"] == 0, (pattern, state)
            assert len(result["drawImage"]) == 24 * 18 * 4
            assert len(result["frames"]) == 4
            for ordinal, frame in enumerate(result["frames"]):
                if frame != result["drawImage"]:
                    mismatches.append({"pattern": pattern, "frame": ordinal,
                                       **_pixel_difference(result["drawImage"], frame)})
        if mismatches:
            pytest.fail("captureStream frames differ from drawImage: %r" % mismatches)
    finally:
        page.close()

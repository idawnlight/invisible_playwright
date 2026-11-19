// The probe of tests/test_the_bytes_a_page_reads_do_not_change_e2e.py: one expression that resolves to a flat object
// {key: {s: "a short string"} | {h, n, s0} | {h, n, w, b64}} of everything a page can read about drawing and text:
//   c2d.*   canvas 2D: getImageData bytes, toDataURL (png, jpeg, webp), toBlob, OffscreenCanvas.convertToBlob, measureText; drawImage scaled up
//           and down with imageSmoothing off and on at every imageSmoothingQuality (c2d.scale.*); gradients (linear, radial, conic) filling and
//           stroking text (c2d.gradtext)
//   gl1.*   WebGL 1 and gl2.* WebGL 2: strings, extensions, limits, precision formats, readPixels bytes of several scenes, toDataURL, drawImage of the canvas
//   worker.* an OffscreenCanvas WebGL context in a worker
//   gpu.*   the WebGPU adapter
//   dom.*   text layout: the boxes of spans in many fonts, wrapped paragraphs, system colours
//   env.*   the values the persona declares around it
// A PNG the page reads (toDataURL, toBlob, convertToBlob) keeps its bytes in `b64`: the test compares the pixels it decodes to, because the engine
// writes a per-launch ancillary chunk into every PNG (tests/_page_bytes.py). Every scene is wrapped: an exception becomes `<scene>.error` and is
// compared like any other value, so every launch gives the same keys.
// Taken from the hunt that measured the Windows reference (scripts/hunt_page_bytes_probe.js of run 43652292533), with its encoding mode removed.
(async () => {
  const out = {};
  const enc = new TextEncoder();
  function hashBytes(u8) {   // cyrb53 over bytes: no crypto.subtle (it needs a secure context)
    let h1 = 0xdeadbeef ^ u8.length, h2 = 0x41c6ce57 ^ u8.length;
    for (let i = 0; i < u8.length; i++) { const ch = u8[i]; h1 = Math.imul(h1 ^ ch, 2654435761); h2 = Math.imul(h2 ^ ch, 1597334677); }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16);
  }
  function b64(u8) {
    let s = "";
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function put(k, v, w) {
    if (ArrayBuffer.isView(v) || v instanceof ArrayBuffer) {
      const u8 = v instanceof ArrayBuffer ? new Uint8Array(v) : new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
      out[k] = { h: hashBytes(u8), n: u8.length };
      if (w) { out[k].w = w; out[k].bpp = 4 * (v.BYTES_PER_ELEMENT || 1); }   // pixels per row, bytes per pixel (RGBA)
      if (u8.length <= 400 * 1024) out[k].b64 = b64(u8);
    } else {
      const s = typeof v === "string" ? v : JSON.stringify(v);
      out[k] = s.length <= 3000 ? { s } : { h: hashBytes(enc.encode(s)), n: s.length, s0: s.slice(0, 200) };
      if (s.length > 3000 && s.startsWith("data:image/png;base64,") && s.length <= 600 * 1024) out[k].b64 = s.slice(22);
    }
  }
  async function scene(k, fn) {
    try { await fn(); } catch (e) { put(k + ".error", String((e && e.message) || e)); }
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ------------------------------------------------------------------------------------------------------------ env
  await scene("env", async () => {
    put("env.ua", navigator.userAgent);
    put("env.platform", navigator.platform);
    put("env.oscpu", navigator.oscpu || "");
    put("env.hw", navigator.hardwareConcurrency);
    put("env.lang", navigator.languages);
    put("env.dpr", window.devicePixelRatio);
    put("env.screen", [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth, screen.pixelDepth]);
    put("env.mq", ["(color-gamut: srgb)", "(color-gamut: p3)", "(dynamic-range: high)", "(prefers-color-scheme: dark)", "(prefers-reduced-motion: reduce)",
                   "(forced-colors: active)", "(inverted-colors: inverted)"].map((q) => matchMedia(q).matches));
  });

  // ---------------------------------------------------------------------------------------------------- canvas 2D
  function c2d(w, h) { const c = document.createElement("canvas"); c.width = w; c.height = h; return [c, c.getContext("2d")]; }
  function dataUrlBytes(url) {
    const bin = atob(url.split(",")[1]);
    const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u;
  }
  async function read2d(k, c, g) {
    put(k + ".getImageData", g.getImageData(0, 0, c.width, c.height).data, c.width);
    put(k + ".png", c.toDataURL());
    put(k + ".jpeg", c.toDataURL("image/jpeg", 0.8));
    put(k + ".webp", c.toDataURL("image/webp", 0.8));
    const blob = await new Promise((r) => c.toBlob(r));
    put(k + ".toBlob", blob ? new Uint8Array(await blob.arrayBuffer()) : "null");
  }
  await scene("c2d.shapes", async () => {
    const [c, g] = c2d(256, 160);
    g.fillStyle = "#fff"; g.fillRect(0, 0, 256, 160);
    const lg = g.createLinearGradient(0, 0, 256, 0);
    lg.addColorStop(0, "#f00"); lg.addColorStop(0.5, "rgba(0,200,0,.5)"); lg.addColorStop(1, "#00f");
    g.fillStyle = lg; g.fillRect(8, 8, 240, 40);
    const rg = g.createRadialGradient(60, 100, 4, 60, 100, 50);
    rg.addColorStop(0, "#ff0"); rg.addColorStop(1, "rgba(255,0,255,.2)");
    g.fillStyle = rg; g.beginPath(); g.arc(60, 100, 50, 0, Math.PI * 2); g.fill();
    g.strokeStyle = "#123"; g.lineWidth = 1.5; g.beginPath(); g.moveTo(130, 60); g.bezierCurveTo(160, 20, 200, 140, 240, 70); g.stroke();
    g.lineWidth = 0.7; g.beginPath(); g.ellipse(190, 110, 40, 20, 0.5, 0, Math.PI * 2); g.stroke();
    g.save(); g.shadowColor = "rgba(0,0,0,.6)"; g.shadowBlur = 6; g.shadowOffsetX = 3; g.shadowOffsetY = 3; g.fillStyle = "#38c"; g.fillRect(120, 100, 50, 30); g.restore();
    g.save(); g.translate(30, 140); g.rotate(0.3); g.scale(1.3, 0.8); g.fillStyle = "#a40"; g.fillRect(0, 0, 40, 20); g.restore();
    if (g.createConicGradient) {
      const cg = g.createConicGradient(0.5, 215, 140);
      cg.addColorStop(0, "#f80"); cg.addColorStop(0.5, "#08f"); cg.addColorStop(1, "#f80");
      g.fillStyle = cg; g.fillRect(190, 120, 60, 36);
    }
    await read2d("c2d.shapes", c, g);
  });
  await scene("c2d.composite", async () => {
    const ops = ["source-over", "source-in", "source-out", "source-atop", "destination-over", "destination-in", "destination-out", "destination-atop",
                 "lighter", "copy", "xor", "multiply", "screen", "overlay", "darken", "lighten", "color-dodge", "color-burn", "hard-light", "soft-light",
                 "difference", "exclusion", "hue", "saturation", "color", "luminosity"];
    const [c, g] = c2d(8 * 32, 4 * 32);
    ops.forEach((op, i) => {
      const x = (i % 8) * 32, y = Math.floor(i / 8) * 32;
      g.save(); g.beginPath(); g.rect(x, y, 32, 32); g.clip();
      g.globalCompositeOperation = "source-over";
      g.fillStyle = "rgba(200,60,40,.8)"; g.fillRect(x + 2, y + 2, 20, 20);
      g.globalCompositeOperation = op;
      g.fillStyle = "rgba(30,90,220,.6)"; g.fillRect(x + 10, y + 10, 20, 20);
      g.restore();
    });
    await read2d("c2d.composite", c, g);
  });
  await scene("c2d.filter_draw_image", async () => {
    const [c, g] = c2d(160, 120);
    const [s, sg] = c2d(10, 10);
    for (let i = 0; i < 100; i++) { sg.fillStyle = "hsl(" + ((i * 37) % 360) + ",70%,50%)"; sg.fillRect(i % 10, Math.floor(i / 10), 1, 1); }
    g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
    g.drawImage(s, 0, 0, 10, 10, 5.5, 5.5, 70.3, 70.3);
    g.imageSmoothingEnabled = false; g.drawImage(s, 80, 5, 60, 60);
    if ("filter" in g) { g.filter = "blur(2px) saturate(1.4)"; g.drawImage(s, 5, 70, 40, 40); g.filter = "none"; }
    const p = g.createPattern(s, "repeat"); g.fillStyle = p; g.fillRect(60, 70, 90, 40);
    const d = g.getImageData(0, 0, 40, 40); g.putImageData(d, 100, 80);
    await read2d("c2d.image", c, g);
  });
  const FONTS = ["16px sans-serif", "20px serif", "18px monospace", "bold 16px Arial", "italic 15px 'Segoe UI'", "16px 'Times New Roman'", "16px Calibri",
                 "14px Consolas", "16px Verdana", "16px Tahoma", "17px Georgia", "16px 'Segoe UI Emoji'", "16px 'Microsoft YaHei'", "16px 'Yu Gothic'"];
  const TEXTS = ["Hamburgefonstiv 0123456789", "The quick brown fox jumps over the lazy dog", "ÀÉÎÕÜ ß ﬁ ﬂ ffl", "日本語のテキスト 漢字", "😀🚀👍🏽", "مرحبا بالعالم"];
  await scene("c2d.text", async () => {
    const [c, g] = c2d(520, FONTS.length * 28 + 20);
    g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
    FONTS.forEach((f, i) => {
      g.font = f; g.fillStyle = "#101820"; g.textBaseline = "alphabetic";
      g.fillText(TEXTS[i % TEXTS.length], 6, 22 + i * 28);
      g.strokeStyle = "rgba(200,0,0,.8)"; g.lineWidth = 0.6; g.strokeText("Ag", 470, 22 + i * 28);
    });
    await read2d("c2d.text", c, g);
    const m = [];
    for (const f of FONTS) {
      g.font = f;
      for (const t of TEXTS) {
        const r = g.measureText(t);
        m.push([f, t, r.width, r.actualBoundingBoxLeft, r.actualBoundingBoxRight, r.actualBoundingBoxAscent, r.actualBoundingBoxDescent,
                r.fontBoundingBoxAscent, r.fontBoundingBoxDescent]);
      }
    }
    put("c2d.measure", m);
  });
  await scene("c2d.offscreen", async () => {
    if (typeof OffscreenCanvas === "undefined") { put("c2d.offscreen", "no OffscreenCanvas"); return; }
    const oc = new OffscreenCanvas(160, 80), g = oc.getContext("2d");
    g.fillStyle = "#fed"; g.fillRect(0, 0, 160, 80); g.fillStyle = "rgba(10,120,200,.7)"; g.beginPath(); g.arc(50, 40, 30, 0, 6.2); g.fill();
    g.font = "bold 18px sans-serif"; g.fillStyle = "#222"; g.fillText("offscreen 2d", 70, 45);
    put("c2d.offscreen.getImageData", g.getImageData(0, 0, 160, 80).data, 160);
    const blob = await oc.convertToBlob();
    put("c2d.offscreen.convertToBlob", new Uint8Array(await blob.arrayBuffer()));
  });

  // ----------------------------------------------------------------------------------------- drawImage scaled, smoothing and quality
  // A 24x24 source with hard edges and a ramp, a 200x200 one (to scale down), each drawn up and down, rotated and not, with imageSmoothing off and
  // on at low, medium and high. The pixels (getImageData) and the PNG of the same canvas, which the test compares decoded.
  await scene("c2d.scale", async () => {
    const [s1, sg1] = c2d(24, 24);
    for (let y = 0; y < 24; y++) for (let x = 0; x < 24; x++) {
      sg1.fillStyle = (x + y) % 5 === 0 ? "#000" : "hsl(" + ((x * 13 + y * 7) % 360) + "," + (40 + (y * 2)) + "%," + (30 + x) + "%)";
      sg1.fillRect(x, y, 1, 1);
    }
    const [s2, sg2] = c2d(200, 200);
    for (let i = 0; i < 40; i++) {
      sg2.fillStyle = "hsl(" + (i * 9) + ",70%,50%)"; sg2.fillRect((i * 17) % 190, (i * 29) % 190, 23 + (i % 7), 11 + (i % 5));
      sg2.strokeStyle = i % 2 ? "#fff" : "#111"; sg2.lineWidth = 1; sg2.beginPath(); sg2.moveTo(i * 5, 0); sg2.lineTo(200 - i * 4, 200); sg2.stroke();
    }
    const configs = [["off", false, "low"], ["low", true, "low"], ["medium", true, "medium"], ["high", true, "high"]];
    for (const [name, on, quality] of configs) {
      const [c, g] = c2d(220, 130);
      g.fillStyle = "#fff"; g.fillRect(0, 0, 220, 130);
      g.imageSmoothingEnabled = on; g.imageSmoothingQuality = quality;
      g.drawImage(s1, 0, 0, 24, 24, 3.25, 3.25, 97.3, 97.3);            // up, not an integer factor
      g.drawImage(s2, 0, 0, 200, 200, 110.5, 4.5, 37, 37);              // down, far
      g.drawImage(s2, 20, 20, 120, 120, 150, 4, 61, 61);                // down, near, a part of the source
      g.drawImage(s1, 0, 0, 24, 24, 110, 50, 24, 24);                   // 1:1 at a whole pixel
      g.save(); g.translate(180, 95); g.rotate(0.35); g.drawImage(s1, 0, 0, 24, 24, -20, -20, 40, 40); g.restore();   // rotated and scaled up
      g.save(); g.translate(60, 118); g.scale(1.7, 0.45); g.drawImage(s2, 0, 0, 200, 200, 0, -20, 30, 30); g.restore(); // scaled by a transform
      put("c2d.scale." + name + ".getImageData", g.getImageData(0, 0, 220, 130).data, 220);
      put("c2d.scale." + name + ".png", c.toDataURL());
    }
  });
  // Text filled and stroked with gradients of the three kinds, with a shadow and with a global alpha: the paths the canvas takes for a gradient
  // over glyphs.
  await scene("c2d.gradtext", async () => {
    const [c, g] = c2d(420, 210);
    g.fillStyle = "#fff"; g.fillRect(0, 0, 420, 210);
    const lin = g.createLinearGradient(10, 0, 400, 0);
    lin.addColorStop(0, "#d00"); lin.addColorStop(0.4, "#fc0"); lin.addColorStop(0.7, "rgba(0,160,60,.6)"); lin.addColorStop(1, "#04d");
    const rad = g.createRadialGradient(100, 90, 4, 130, 100, 90);
    rad.addColorStop(0, "#fff"); rad.addColorStop(0.5, "#c0c"); rad.addColorStop(1, "#036");
    const rad2 = g.createRadialGradient(300, 60, 30, 340, 80, 8);   // the focal circle outside the start circle
    rad2.addColorStop(0, "#f60"); rad2.addColorStop(1, "#0af");
    const con = g.createConicGradient ? g.createConicGradient(0.6, 210, 150) : lin;
    con.addColorStop(0, "#f80"); con.addColorStop(0.33, "#0c8"); con.addColorStop(0.66, "#80f"); con.addColorStop(1, "#f80");
    g.textBaseline = "alphabetic";
    g.font = "bold 40px sans-serif"; g.fillStyle = lin; g.fillText("Gradient 0123", 10, 44);
    g.font = "italic 34px serif"; g.fillStyle = rad; g.fillText("Hamburgefonstiv", 10, 90);
    g.font = "28px monospace"; g.fillStyle = rad2; g.fillText("radial focal 1/2", 220, 40);
    g.font = "bold 44px Arial"; g.strokeStyle = lin; g.lineWidth = 2.5; g.strokeText("Stroke", 10, 140);
    g.font = "36px 'Times New Roman'"; g.fillStyle = con; g.fillText("Conic text", 150, 150);
    g.save(); g.shadowColor = "rgba(0,0,0,.55)"; g.shadowBlur = 4; g.shadowOffsetX = 2; g.shadowOffsetY = 3; g.globalAlpha = 0.8;
    g.font = "30px 'Segoe UI'"; g.fillStyle = lin; g.fillText("Shadow and alpha", 10, 190); g.restore();
    g.font = "20px sans-serif"; g.fillStyle = con; g.fillRect(300, 150, 110, 50); g.fillStyle = "#fff"; g.fillText("on conic", 310, 180);
    put("c2d.gradtext.getImageData", g.getImageData(0, 0, 420, 210).data, 420);
    put("c2d.gradtext.png", c.toDataURL());
  });

  // ------------------------------------------------------------------------------------------------------------ WebGL
  const P1 = ["VERSION", "SHADING_LANGUAGE_VERSION", "VENDOR", "RENDERER", "MAX_TEXTURE_SIZE", "MAX_CUBE_MAP_TEXTURE_SIZE", "MAX_RENDERBUFFER_SIZE",
              "MAX_VIEWPORT_DIMS", "ALIASED_LINE_WIDTH_RANGE", "ALIASED_POINT_SIZE_RANGE", "MAX_VERTEX_ATTRIBS", "MAX_VARYING_VECTORS",
              "MAX_COMBINED_TEXTURE_IMAGE_UNITS", "MAX_VERTEX_TEXTURE_IMAGE_UNITS", "MAX_TEXTURE_IMAGE_UNITS", "MAX_VERTEX_UNIFORM_VECTORS",
              "MAX_FRAGMENT_UNIFORM_VECTORS", "RED_BITS", "GREEN_BITS", "BLUE_BITS", "ALPHA_BITS", "DEPTH_BITS", "STENCIL_BITS", "SAMPLES",
              "SAMPLE_BUFFERS", "SUBPIXEL_BITS"];
  const P2 = ["MAX_3D_TEXTURE_SIZE", "MAX_ARRAY_TEXTURE_LAYERS", "MAX_COLOR_ATTACHMENTS", "MAX_DRAW_BUFFERS", "MAX_ELEMENTS_INDICES", "MAX_ELEMENTS_VERTICES",
              "MAX_FRAGMENT_INPUT_COMPONENTS", "MAX_FRAGMENT_UNIFORM_BLOCKS", "MAX_FRAGMENT_UNIFORM_COMPONENTS", "MAX_PROGRAM_TEXEL_OFFSET", "MAX_SAMPLES",
              "MAX_SERVER_WAIT_TIMEOUT", "MAX_TEXTURE_LOD_BIAS", "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS", "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS",
              "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS", "MAX_UNIFORM_BLOCK_SIZE", "MAX_UNIFORM_BUFFER_BINDINGS", "MAX_VARYING_COMPONENTS",
              "MAX_VERTEX_OUTPUT_COMPONENTS", "MAX_VERTEX_UNIFORM_BLOCKS", "MAX_VERTEX_UNIFORM_COMPONENTS", "MIN_PROGRAM_TEXEL_OFFSET",
              "MAX_COMBINED_UNIFORM_BLOCKS", "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS", "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS", "UNIFORM_BUFFER_OFFSET_ALIGNMENT"];
  function glInfo(p, gl, v2) {
    const params = {};
    for (const n of P1.concat(v2 ? P2 : [])) {
      try { let v = gl.getParameter(gl[n]); if (ArrayBuffer.isView(v)) v = Array.from(v); params[n] = v; } catch (e) { params[n] = "ERR " + e.message; }
    }
    put(p + ".params", params);
    const exts = gl.getSupportedExtensions() || [];
    put(p + ".extensions", exts);
    const eo = [];
    for (const e of exts) {
      try { const o = gl.getExtension(e); eo.push([e, o ? Object.keys(Object.getPrototypeOf(o)).length : -1]); } catch (er) { eo.push([e, "ERR"]); }
    }
    put(p + ".extension_objects", eo);
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    put(p + ".unmasked", dbg ? [gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL), gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)] : "no WEBGL_debug_renderer_info");
    const an = gl.getExtension("EXT_texture_filter_anisotropic");
    put(p + ".anisotropy", an ? gl.getParameter(an.MAX_TEXTURE_MAX_ANISOTROPY_EXT) : "none");
    const prec = [];
    for (const sh of ["VERTEX_SHADER", "FRAGMENT_SHADER"]) for (const lv of ["LOW_FLOAT", "MEDIUM_FLOAT", "HIGH_FLOAT", "LOW_INT", "MEDIUM_INT", "HIGH_INT"]) {
      const f = gl.getShaderPrecisionFormat(gl[sh], gl[lv]);
      prec.push([sh, lv, f && f.rangeMin, f && f.rangeMax, f && f.precision]);
    }
    put(p + ".precision", prec);
    put(p + ".attributes", gl.getContextAttributes());
  }
  function program(gl, vs, fs) {
    const mk = (type, src) => {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error("shader: " + gl.getShaderInfoLog(s));
      return s;
    };
    const pr = gl.createProgram();
    gl.attachShader(pr, mk(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, mk(gl.FRAGMENT_SHADER, fs));
    gl.bindAttribLocation(pr, 0, "p"); gl.bindAttribLocation(pr, 1, "c");
    gl.linkProgram(pr);
    if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error("link: " + gl.getProgramInfoLog(pr));
    gl.useProgram(pr);
    return pr;
  }
  function buffer(gl, loc, data, size) {
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
    return b;
  }
  function pixels(gl, w, h) { const u = new Uint8Array(w * h * 4); gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, u); return u; }
  const VS = "attribute vec2 p; attribute vec4 c; varying vec4 v; varying vec2 uv; void main(){ v=c; uv=p*0.5+0.5; gl_Position=vec4(p,0.,1.); gl_PointSize=7.5; }";
  const FS_COLOR = "precision mediump float; varying vec4 v; void main(){ gl_FragColor=v; }";
  const QUAD = [-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1];

  async function glScenes(p, v2) {
    const kind = v2 ? "webgl2" : "webgl";
    const mkctx = (w, h, attrs) => {
      const c = document.createElement("canvas"); c.width = w; c.height = h;
      return [c, c.getContext(kind, Object.assign({ preserveDrawingBuffer: true }, attrs || {}))];
    };
    let [c0, gl0] = mkctx(64, 64);
    if (!gl0) { put(p + ".context", "null"); return; }
    glInfo(p, gl0, v2);
    await scene(p + ".triangle", async () => {
      const [c, gl] = mkctx(96, 96, { antialias: false });
      gl.viewport(0, 0, 96, 96); gl.clearColor(0.1, 0.2, 0.3, 0.4); gl.clear(gl.COLOR_BUFFER_BIT);
      program(gl, VS, FS_COLOR);
      buffer(gl, 0, [-0.8, -0.7, 0.9, -0.5, -0.1, 0.85], 2);
      buffer(gl, 1, [1, 0, 0, 1, 0, 1, 0, 0.7, 0, 0, 1, 0.3], 4);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      put(p + ".triangle.readPixels", pixels(gl, 96, 96), 96);
      put(p + ".triangle.toDataURL", c.toDataURL());
      put(p + ".triangle.toDataURL_jpeg", c.toDataURL("image/jpeg", 0.8));
      const [d, g2] = c2d(96, 96); g2.drawImage(c, 0, 0);
      put(p + ".triangle.drawImage", g2.getImageData(0, 0, 96, 96).data, 96);
    });
    await scene(p + ".antialias", async () => {
      for (const aa of [true, false]) {
        const [c, gl] = mkctx(96, 96, { antialias: aa });
        gl.viewport(0, 0, 96, 96); gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
        program(gl, VS, FS_COLOR);
        buffer(gl, 0, [-0.83, -0.71, 0.77, -0.43, -0.1, 0.89, 0.3, 0.9, 0.9, 0.2, 0.6, 0.95], 2);
        buffer(gl, 1, [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0.8, 0, 0, 1, 0.8, 0, 0, 1, 0.8, 0, 0, 1], 4);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
        put(p + ".antialias." + aa + ".samples", gl.getParameter(gl.SAMPLES));
        put(p + ".antialias." + aa + ".readPixels", pixels(gl, 96, 96), 96);
      }
    });
    for (const prec of ["highp", "mediump"]) await scene(p + ".math." + prec, async () => {
      const [c, gl] = mkctx(64, 64, { antialias: false });
      const fp = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT);
      const use = prec === "highp" && !(fp && fp.precision) ? "mediump" : prec;
      gl.viewport(0, 0, 64, 64);
      program(gl, VS, "precision " + use + " float; varying vec2 uv;" +
        "void main(){ float x=uv.x*6.0; gl_FragColor=vec4(fract(sin(x)*43758.5453), fract(exp(uv.y*3.0)), fract(pow(uv.x+0.5, 2.2+uv.y)), fract(sqrt(uv.x*uv.y)*17.0)); }");
      buffer(gl, 0, QUAD, 2); gl.drawArrays(gl.TRIANGLES, 0, 6);
      put(p + ".math." + prec + ".readPixels", pixels(gl, 64, 64), 64);
    });
    await scene(p + ".texture", async () => {
      const [c, gl] = mkctx(64, 64, { antialias: false });
      gl.viewport(0, 0, 64, 64);
      program(gl, VS, "precision mediump float; varying vec2 uv; uniform sampler2D t; void main(){ gl_FragColor=texture2D(t, uv*0.9+0.03); }");
      const tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tex);
      const d = new Uint8Array(32 * 32 * 4); let s = 12345;
      for (let i = 0; i < d.length; i++) { s = (s * 1103515245 + 12345) & 0x7fffffff; d[i] = (s >> 16) & 255; }
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 32, 32, 0, gl.RGBA, gl.UNSIGNED_BYTE, d);
      gl.generateMipmap(gl.TEXTURE_2D);
      for (const [name, mag, min] of [["linear", gl.LINEAR, gl.LINEAR], ["mip", gl.LINEAR, gl.LINEAR_MIPMAP_LINEAR], ["nearest", gl.NEAREST, gl.NEAREST]]) {
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, mag); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, min);
        gl.clear(gl.COLOR_BUFFER_BIT);
        buffer(gl, 0, QUAD, 2); gl.drawArrays(gl.TRIANGLES, 0, 6);
        put(p + ".texture." + name + ".readPixels", pixels(gl, 64, 64), 64);
      }
    });
    await scene(p + ".blend", async () => {
      const [c, gl] = mkctx(64, 64, { antialias: false, premultipliedAlpha: false });
      gl.viewport(0, 0, 64, 64); gl.clearColor(0.2, 0.4, 0.6, 0.5); gl.clear(gl.COLOR_BUFFER_BIT);
      program(gl, VS, FS_COLOR); gl.enable(gl.BLEND);
      for (const [src, dst] of [[gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA], [gl.ONE, gl.ONE_MINUS_SRC_ALPHA], [gl.DST_COLOR, gl.ZERO]]) {
        gl.blendFunc(src, dst);
        buffer(gl, 0, [-0.9, -0.9, 0.3, -0.5, -0.3, 0.7], 2);
        buffer(gl, 1, [0.9, 0.2, 0.1, 0.37, 0.1, 0.8, 0.3, 0.55, 0.2, 0.3, 0.9, 0.21], 4);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
      put(p + ".blend.readPixels", pixels(gl, 64, 64), 64);
    });
    await scene(p + ".lines_points", async () => {
      const [c, gl] = mkctx(64, 64, { antialias: false });
      gl.viewport(0, 0, 64, 64); gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      program(gl, VS, FS_COLOR);
      buffer(gl, 1, [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], 4);
      buffer(gl, 0, [-0.9, -0.3, 0.9, 0.5, -0.9, 0.6, 0.2, -0.9], 2);
      gl.lineWidth(1); gl.drawArrays(gl.LINES, 0, 4);
      gl.lineWidth(3); buffer(gl, 0, [-0.6, -0.8, 0.8, 0.9], 2); gl.drawArrays(gl.LINES, 0, 2);
      buffer(gl, 0, [-0.5, 0.5, 0.4, -0.2], 2); gl.drawArrays(gl.POINTS, 0, 2);
      put(p + ".lines_points.readPixels", pixels(gl, 64, 64), 64);
    });
    await scene(p + ".depth_stencil", async () => {
      const [c, gl] = mkctx(64, 64, { antialias: false, depth: true, stencil: true });
      gl.viewport(0, 0, 64, 64); gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT | gl.STENCIL_BUFFER_BIT);
      program(gl, "attribute vec3 p; attribute vec4 c; varying vec4 v; void main(){ v=c; gl_Position=vec4(p,1.); }", FS_COLOR);
      gl.enable(gl.DEPTH_TEST); gl.enable(gl.STENCIL_TEST); gl.stencilFunc(gl.ALWAYS, 1, 255); gl.stencilOp(gl.KEEP, gl.KEEP, gl.REPLACE);
      buffer(gl, 0, [-0.9, -0.9, 0.2, 0.9, -0.5, -0.4, 0.0, 0.9, 0.6], 3);
      buffer(gl, 1, [1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1], 4);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      buffer(gl, 0, [-0.9, 0.9, 0.0, 0.9, 0.9, 0.0, 0.0, -0.9, 0.5], 3);
      buffer(gl, 1, [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1], 4);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      put(p + ".depth_stencil.readPixels", pixels(gl, 64, 64), 64);
    });
    if (v2) {
      await scene(p + ".msaa_resolve", async () => {
        const [c, gl] = mkctx(64, 64, { antialias: false });
        const rb = gl.createRenderbuffer(); gl.bindRenderbuffer(gl.RENDERBUFFER, rb);
        gl.renderbufferStorageMultisample(gl.RENDERBUFFER, 4, gl.RGBA8, 64, 64);
        const fb = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
        gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, rb);
        const tx = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tx);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 64, 64, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        const fb2 = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fb2);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tx, 0);
        gl.bindFramebuffer(gl.FRAMEBUFFER, fb); gl.viewport(0, 0, 64, 64); gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
        program(gl, VS, FS_COLOR);
        buffer(gl, 0, [-0.83, -0.71, 0.77, -0.43, -0.1, 0.89], 2);
        buffer(gl, 1, [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], 4);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        gl.bindFramebuffer(gl.READ_FRAMEBUFFER, fb); gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, fb2);
        gl.blitFramebuffer(0, 0, 64, 64, 0, 0, 64, 64, gl.COLOR_BUFFER_BIT, gl.NEAREST);
        gl.bindFramebuffer(gl.FRAMEBUFFER, fb2);
        put(p + ".msaa_resolve.readPixels", pixels(gl, 64, 64), 64);
      });
      await scene(p + ".float_target", async () => {
        const [c, gl] = mkctx(32, 32, { antialias: false });
        if (!gl.getExtension("EXT_color_buffer_float")) { put(p + ".float_target", "no EXT_color_buffer_float"); return; }
        const tx = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tx);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, 32, 32, 0, gl.RGBA, gl.FLOAT, null);
        const fb = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tx, 0);
        gl.viewport(0, 0, 32, 32);
        program(gl, VS, "precision highp float; varying vec2 uv; void main(){ gl_FragColor=vec4(sin(uv.x*9.0)*exp(uv.y), pow(uv.x+0.1, 1.7), sqrt(uv.y), uv.x/(uv.y+0.37)); }");
        buffer(gl, 0, QUAD, 2); gl.drawArrays(gl.TRIANGLES, 0, 6);
        const f = new Float32Array(32 * 32 * 4); gl.readPixels(0, 0, 32, 32, gl.RGBA, gl.FLOAT, f);
        put(p + ".float_target.readPixels", f, 32);
      });
    }
  }
  await scene("gl1", () => glScenes("gl1", false));
  await scene("gl2", () => glScenes("gl2", true));

  await scene("worker.webgl", async () => {
    if (typeof Worker === "undefined" || typeof OffscreenCanvas === "undefined") { put("worker.webgl", "no worker or OffscreenCanvas"); return; }
    const src = "onmessage = () => { try { const c = new OffscreenCanvas(32, 32), gl = c.getContext('webgl'); if (!gl) { postMessage({err: 'no webgl'}); return; }" +
      "const d = gl.getExtension('WEBGL_debug_renderer_info');" +
      "gl.clearColor(0.25, 0.5, 0.75, 1); gl.clear(gl.COLOR_BUFFER_BIT); const u = new Uint8Array(32*32*4); gl.readPixels(0,0,32,32,gl.RGBA,gl.UNSIGNED_BYTE,u);" +
      "postMessage({vendor: gl.getParameter(gl.VENDOR), renderer: gl.getParameter(gl.RENDERER), unmasked: d ? [gl.getParameter(d.UNMASKED_VENDOR_WEBGL), gl.getParameter(d.UNMASKED_RENDERER_WEBGL)] : null," +
      "exts: gl.getSupportedExtensions(), px: Array.from(u.subarray(0, 16))}); } catch (e) { postMessage({err: String(e)}); } };";
    const w = new Worker(URL.createObjectURL(new Blob([src])));
    const r = await Promise.race([new Promise((res) => { w.onmessage = (e) => res(e.data); w.onerror = (e) => res({ err: "worker error " + e.message }); w.postMessage(1); }),
                                  sleep(10000).then(() => ({ err: "timeout" }))]);
    w.terminate();
    put("worker.webgl", r);
  });
  await scene("gpu.webgpu", async () => {
    if (!navigator.gpu) { put("gpu.webgpu", "no navigator.gpu"); return; }
    const a = await Promise.race([navigator.gpu.requestAdapter(), sleep(10000).then(() => "timeout")]);
    if (!a || a === "timeout") { put("gpu.webgpu", String(a)); return; }
    const info = a.info || (a.requestAdapterInfo ? await a.requestAdapterInfo() : {});
    // Features and limits are read by NAME, never by iterating them: the expression runs with the page's objects behind Xray wrappers, and
    // `Array.from(a.features)` / `for (k in a.limits)` threw "Xray wrapping of iterators not supported" there (run 43652292533). `has` and a property
    // read are plain calls. The names are those of the WebGPU specification (limits table and GPUFeatureName); one the engine does not know reads
    // false / undefined, which is a value like any other.
    const featureNames = ["depth-clip-control", "depth32float-stencil8", "texture-compression-bc", "texture-compression-bc-sliced-3d",
      "texture-compression-etc2", "texture-compression-astc", "texture-compression-astc-sliced-3d", "timestamp-query", "indirect-first-instance",
      "shader-f16", "rg11b10ufloat-renderable", "bgra8unorm-storage", "float32-filterable", "float32-blendable", "clip-distances",
      "dual-source-blending", "subgroups"];
    const limitNames = ["maxTextureDimension1D", "maxTextureDimension2D", "maxTextureDimension3D", "maxTextureArrayLayers", "maxBindGroups",
      "maxBindGroupsPlusVertexBuffers", "maxBindingsPerBindGroup", "maxDynamicUniformBuffersPerPipelineLayout",
      "maxDynamicStorageBuffersPerPipelineLayout", "maxSampledTexturesPerShaderStage", "maxSamplersPerShaderStage",
      "maxStorageBuffersPerShaderStage", "maxStorageTexturesPerShaderStage", "maxUniformBuffersPerShaderStage", "maxUniformBufferBindingSize",
      "maxStorageBufferBindingSize", "minUniformBufferOffsetAlignment", "minStorageBufferOffsetAlignment", "maxVertexBuffers", "maxBufferSize",
      "maxVertexAttributes", "maxVertexBufferArrayStride", "maxInterStageShaderVariables", "maxColorAttachments",
      "maxColorAttachmentBytesPerSample", "maxComputeWorkgroupStorageSize", "maxComputeInvocationsPerWorkgroup", "maxComputeWorkgroupSizeX",
      "maxComputeWorkgroupSizeY", "maxComputeWorkgroupSizeZ", "maxComputeWorkgroupsPerDimension"];
    const lim = {}, feat = {};
    for (const k of limitNames) { try { lim[k] = a.limits[k]; } catch (e) { lim[k] = "ERR " + e.message; } }
    for (const f of featureNames) { try { feat[f] = a.features.has(f); } catch (e) { feat[f] = "ERR " + e.message; } }
    put("gpu.webgpu.info", { vendor: info.vendor, architecture: info.architecture, device: info.device, description: info.description });
    put("gpu.webgpu.features", feat);
    put("gpu.webgpu.limits", lim);
  });

  // ------------------------------------------------------------------------------------------------------------ DOM text
  await scene("dom.text", async () => {
    const box = document.createElement("div"); box.style.cssText = "position:absolute;left:0;top:0;white-space:nowrap";
    document.body.appendChild(box);
    const rows = [];
    for (const f of FONTS) for (const t of TEXTS) {
      const s = document.createElement("span"); s.style.font = f; s.textContent = t; box.appendChild(s);
      const r = s.getBoundingClientRect();
      rows.push([f, t, r.width, r.height, s.offsetWidth, s.offsetHeight]);
      box.removeChild(s);
    }
    put("dom.span_boxes", rows);
    const para = [];
    for (const f of ["16px sans-serif", "15px 'Times New Roman'", "14px Consolas"]) {
      const d = document.createElement("div"); d.style.cssText = "width:200px;font:" + f; d.textContent = TEXTS.join(" · ") + " " + TEXTS.join(" · ");
      box.appendChild(d); const r = d.getBoundingClientRect(); para.push([f, r.width, r.height]); box.removeChild(d);
    }
    put("dom.paragraphs", para);
    put("dom.fonts_check", FONTS.map((f) => [f, document.fonts.check(f)]));
    const colors = ["Canvas", "CanvasText", "ButtonFace", "ButtonText", "ButtonBorder", "Field", "FieldText", "Highlight", "HighlightText", "GrayText", "LinkText",
                    "VisitedText", "ActiveText", "Mark", "MarkText", "AccentColor", "AccentColorText", "SelectedItem", "SelectedItemText"];
    put("dom.system_colors", colors.map((c) => { const e = document.createElement("i"); e.style.color = c; box.appendChild(e); const v = getComputedStyle(e).color; box.removeChild(e); return [c, v]; }));
    document.body.removeChild(box);
  });
  return out;
})()

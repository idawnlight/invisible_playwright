"""A screenshot draws the text of the bundled fonts with the outlines of those fonts (e2e, every OS, from firefox-66).

`page.screenshot` is not taken from the screen: the parent process replays what the content process recorded, and a text run in that
recording carries its font as a path, which the parent must turn into a font again. On Linux the parent asked fontconfig for it, and
fontconfig does not know the folder of the bundle: it answered another font (DejaVu Sans), and the run was drawn with the glyph NUMBERS
of the bundled font and the OUTLINES of that one. Latin text came out in the shape of DejaVu (Times New Roman without serifs); accents,
the typographic apostrophe, the euro sign, Chinese, Arabic and Russian came out as other glyphs. The screen, the canvas 2D and
`measureText` were right: the defect is only in what the parent rebuilds. Retail Firefox on Windows has no such step to get wrong in
this way, so whether Windows has the defect is what this test answers there, with the same page and the same assertion.

The reference is not a stored image and not a threshold. One page holds, for a family of the bundle, the same block of text twice:

  * as HTML (``#dom``), which the screenshot rebuilds in the parent;
  * as a ``<canvas>`` (``#cv``) onto which the content process drew an SVG image with a ``foreignObject`` holding the same markup
    (the same string, the same styles), after the image had loaded: the pixels of that box are made in the content process and the
    parent only copies them.

Both are in the SAME ``page.screenshot``, at integer positions of a device pixel ratio of 1, and the test cuts the two boxes out of the
PNG and asks that they are the same bytes. A parent that rebuilds the text with another font cannot give the bytes of one that does not.
The blocks are black on white, drawn with greyscale antialiasing (the persona's text quality is pinned to antialiased, not ClearType,
which an SVG image does not draw), so a difference is a difference of outline and not of the colour of an edge.

One case for each family, so the failure says which one: Arial, Times New Roman, Consolas and Segoe UI, the four faces of the bundle that
the persona answers to on every Windows. Each block holds accents (é è à ü), the typographic apostrophe, the euro sign and the
quotation marks, Chinese, Arabic and Russian.

Two more cases are a face of a collection that is NOT the first face of its file: MS PGothic (face 2 of msgothic5_32.ttc; face 0 is MS
Gothic) and NSimSun (face 1 of simsun.ttc; face 0 is SimSun). A text run carries the path of the file AND the index of the face, and a
parent that rebuilds the font from the path alone, or that lets the system choose the index, draws the first face (or another) of the
same file. Their blocks hold the writing those faces draw (Japanese kana and kanji with Latin letters for MS PGothic, whose spacing is
proportional where MS Gothic's is not; Chinese for NSimSun), so the wrong face gives other outlines and the bytes differ.

Evidence, whether the case passes or fails: when ``RUNNER_TEMP`` is set (a GitHub runner) the case writes the screenshot, the two boxes
as PNG files and a ``result.json`` (family, operating system, engine, sizes, sha256 of each box, how many pixels differ and where, and how
many pixels of each box are not grey) under ``$RUNNER_TEMP/invpw-evidence/screenshot-bundle-fonts/<family>/``, which the e2e jobs upload.
Outside a runner it writes nothing.

Run: pytest tests/test_a_screenshot_draws_the_text_of_the_bundled_fonts_as_the_content_does_e2e.py -m e2e -v
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

import _page_bytes as pb

pytestmark = pytest.mark.e2e

OS_NAME = "windows" if sys.platform == "win32" else "linux"

#: The families the persona declares and the bundle holds (the `f|` rows of the manifest in invisible_core: Arial, Times New Roman,
#: Consolas and Segoe UI each have their files; MS PGothic and NSimSun are a face that is not the first of its .ttc, rows `f|msgothic5_32.ttc|2|`
#: and `f|simsun.ttc|1|`).
FAMILIES = ["Arial", "Times New Roman", "Consolas", "Segoe UI", "MS PGothic", "NSimSun"]

#: The device pixel ratio is 1 and the text is greyscale: see the docstring. 4 is ANTIALIASED_QUALITY (5 is ClearType).
PIN = {"screen.dpr": 1.0, "font.system_text_quality": 4}

#: Where the evidence goes under $RUNNER_TEMP; the workflows upload this folder by this name.
EVIDENCE_FOLDER = "invpw-evidence"

BLOCK_W, BLOCK_H = 480, 214
VIEWPORT = {"width": 520, "height": 480}

PAGE = """<!doctype html><meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: #fff; }
  #dom, #cv { position: absolute; left: 10px; display: block; }
  #dom { top: 10px; width: %(w)dpx; height: %(h)dpx; }
  #cv { top: %(cv_top)dpx; }
</style>
<div id="dom"></div>
<canvas id="cv" width="%(w)d" height="%(h)d"></canvas>
<script>
const W = %(w)d, H = %(h)d;
// ONE string is the block, as HTML in the page and inside the SVG image. Everything that decides how the text is drawn is in it:
// the family, the size, the line height, the languages (which choose the fallback font of the Han and Arabic runs) and the colours.
function markup(family) {
  // The families that are a face of a .ttc that is NOT the first of its collection hold the writing that face draws, so that the
  // face of the collection the parent rebuilds is the one whose outlines are in the block: MS PGothic is face 2 of msgothic5_32.ttc
  // (face 0 is MS Gothic, which draws the same kana with another spacing) and NSimSun is face 1 of simsun.ttc (face 0 is SimSun).
  const OWN = {
    'MS PGothic': [
      '<div lang="en">Hamburgefonstiv 0123456789 fi fl</div>',
      '<div lang="ja">\\u65e5\\u672c\\u8a9e\\u306e\\u30c6\\u30ad\\u30b9\\u30c8\\u3001\\u3072\\u3089\\u304c\\u306a\\u3068\\u30ab\\u30bf\\u30ab\\u30ca</div>',
      '<div lang="ja">ABC abc iiii llll WWWW \\uff71\\uff72\\uff73\\uff74\\uff75 \\u30ac\\u30ae\\u30b0\\u30b2\\u30b4</div>',
      '<div lang="ja">\\u3072\\u3089\\u304c\\u306a \\u3042\\u3044\\u3046\\u3048\\u304a \\u304b\\u304d\\u304f\\u3051\\u3053 \\u3055\\u3057\\u3059\\u305b\\u305d</div>',
      '<div lang="ja">\\u30ab\\u30bf\\u30ab\\u30ca \\u30a2\\u30a4\\u30a6\\u30a8\\u30aa \\u30ab\\u30ad\\u30af\\u30b1\\u30b3 \\u30b5\\u30b7\\u30b9\\u30bb\\u30bd</div>',
      '<div lang="ja">\\u6f22\\u5b57 \\u6771\\u4eac \\u5927\\u962a \\u65e5\\u672c\\u8a9e \\u6587\\u5b57 \\u8868\\u793a</div>',
    ],
    'NSimSun': [
      '<div lang="en">Hamburgefonstiv 0123456789 fi fl</div>',
      '<div lang="zh-CN">\\u4e2d\\u6587\\u6c49\\u5b57\\u6d4b\\u8bd5\\uff0c\\u7b80\\u4f53\\u4e2d\\u6587\\u6587\\u672c</div>',
      '<div lang="zh-CN">ABC abc iiii llll WWWW \\u4e2d\\u6587 0123</div>',
      '<div lang="zh-CN">\\u4f60\\u597d\\uff0c\\u4e16\\u754c\\uff01\\u4eca\\u5929\\u5929\\u6c14\\u5f88\\u597d\\u3002</div>',
      '<div lang="zh-CN">\\u6c49\\u5b57\\u6d4b\\u8bd5 \\u5b66\\u4e60 \\u5de5\\u4f5c \\u751f\\u6d3b \\u4e2d\\u56fd</div>',
      '<div lang="zh-CN">\\u201c\\u5f15\\u53f7\\u201d\\u3001\\u300a\\u4e66\\u540d\\u300b\\u3001\\uff08\\u62ec\\u53f7\\uff09\\uff1b\\u6807\\u70b9</div>',
    ],
  };
  const lines = OWN[family] || [
    '<div lang="en">Hamburgefonstiv 0123456789 fi fl</div>',
    '<div lang="fr">\\u00e9 \\u00e8 \\u00e0 \\u00fc \\u00c9 \\u00c8 \\u00c0 \\u00dc \\u00f1 \\u00e7 \\u00f8 \\u00df</div>',
    '<div lang="en">It\\u2019s \\u00ab\\u20ac \\u00a9 \\u2013 \\u2026\\u00bb \\u2018x\\u2019 \\u201cy\\u201d</div>',
    '<div lang="zh-CN">\\u4e2d\\u6587\\u6c49\\u5b57\\u6d4b\\u8bd5 \\u6f22\\u5b57</div>',
    '<div lang="ar" dir="rtl">\\u0645\\u0631\\u062d\\u0628\\u0627 \\u0628\\u0627\\u0644\\u0639\\u0627\\u0644\\u0645 \\u0627\\u0644\\u0639\\u0631\\u0628\\u064a</div>',
    '<div lang="ru">\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442, \\u043c\\u0438\\u0440! \\u0421\\u044a\\u0435\\u0448\\u044c \\u0435\\u0449\\u0451</div>',
  ];
  const style = 'box-sizing:border-box;width:' + W + 'px;height:' + H + 'px;padding:5px 8px;overflow:hidden;white-space:nowrap;'
    + 'margin:0;background:#fff;color:#000;font-family:\\'' + family + '\\';font-size:20px;line-height:34px;'
    + 'font-weight:400;font-style:normal;';
  return '<div xmlns="http://www.w3.org/1999/xhtml" style="' + style + '">' + lines.join('') + '</div>';
}

async function draw(family) {
  const html = markup(family);
  document.getElementById('dom').innerHTML = html;
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '">'
    + '<foreignObject x="0" y="0" width="' + W + '" height="' + H + '">' + html + '</foreignObject></svg>';
  const img = new Image();
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = () => reject(new Error('the SVG image did not load'));
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  });
  const ctx = document.getElementById('cv').getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, W, H);
  ctx.drawImage(img, 0, 0);
  await document.fonts.ready;
  // one frame after the drawing, so that what is on the screen is what was drawn
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const box = id => { const r = document.getElementById(id).getBoundingClientRect();
                      return {x: r.x, y: r.y, width: r.width, height: r.height}; };
  return {dpr: window.devicePixelRatio, dom: box('dom'), cv: box('cv'),
          lines: document.getElementById('dom').firstElementChild.childElementCount};
}
</script>
"""


def _page():
    return PAGE % {"w": BLOCK_W, "h": BLOCK_H, "cv_top": 10 + BLOCK_H + 10}


def _slug(family):
    return re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")


def _evidence_dir(family):
    """The folder of this case under $RUNNER_TEMP, or None outside a runner."""
    base = os.environ.get("RUNNER_TEMP")
    if not base:
        return None
    return Path(base) / EVIDENCE_FOLDER / "screenshot-bundle-fonts" / _slug(family)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _dark(image):
    """How many pixels of a box are not the white of the background: a box that is blank is not a text that matches."""
    ch, d = image["channels"], image["data"]
    return sum(1 for i in range(0, len(d), ch) if d[i:i + 3] != b"\xff\xff\xff")


def _integer_box(name, box):
    for key in ("x", "y", "width", "height"):
        assert box[key] == int(box[key]), (
            "the %s box is not on whole device pixels (%s = %r): a block off the pixel grid would be drawn twice with two "
            "positions and the two boxes could differ for that alone" % (name, key, box[key]))
    return {key: int(box[key]) for key in box}


@pytest.mark.timeout(300, method="thread")
@pytest.mark.parametrize("family", FAMILIES)
def test_the_text_of_a_bundled_family_is_drawn_by_the_screenshot_as_the_content_drew_it(firefox_binary, engine_at_least, family):
    engine_at_least("firefox-66")
    from invisible_core.seal import active_seal
    from invisible_playwright import InvisiblePlaywright

    tag = active_seal().tag
    with InvisiblePlaywright(seed=21, binary_path=firefox_binary, headless=True, pin=PIN) as b:
        page = b.new_context(viewport=VIEWPORT).new_page()
        page.set_content(_page())
        page.evaluate("fam => { window.DONE = false; window.ERR = null;"
                      " draw(fam).then(r => { window.R = r; window.DONE = true; },"
                      " e => { window.ERR = String(e); window.DONE = true; }); }", family)
        page.wait_for_function("window.DONE === true", timeout=60_000)
        err = page.evaluate("window.ERR")
        assert err is None, "the page could not draw the two blocks: %s" % err
        measured = page.evaluate("window.R")
        shot = page.screenshot(type="png")

    assert measured["dpr"] == 1, "the device pixel ratio is %r, not 1: the boxes would be resampled" % measured["dpr"]
    dom, cv = _integer_box("DOM", measured["dom"]), _integer_box("canvas", measured["cv"])
    assert (dom["width"], dom["height"]) == (cv["width"], cv["height"]) == (BLOCK_W, BLOCK_H), (dom, cv)
    assert measured["lines"] == 6, "the block has %r lines, not 6" % measured["lines"]

    whole = pb.unfiltered(shot)
    assert (whole["w"], whole["h"]) == (VIEWPORT["width"], VIEWPORT["height"]), (
        "the screenshot is %dx%d for a viewport of %dx%d: the boxes are not where the page measured them"
        % (whole["w"], whole["h"], VIEWPORT["width"], VIEWPORT["height"]))
    dom_img = pb.crop(whole, dom["x"], dom["y"], dom["width"], dom["height"])
    cv_img = pb.crop(whole, cv["x"], cv["y"], cv["width"], cv["height"])
    diff = pb.pixels_differ(dom_img, cv_img)
    identical = dom_img["data"] == cv_img["data"]
    assert identical == (diff["pixels"] == 0)

    result = {
        "test": "screenshot-draws-the-bundled-fonts-as-the-content-does", "family": family, "os": OS_NAME,
        "platform": sys.platform, "engine": tag, "seed": 21, "pin": PIN,
        "viewport": VIEWPORT, "screenshot": {"w": whole["w"], "h": whole["h"], "sha256": _sha(shot)},
        "dom": {"box": dom, "sha256": _sha(dom_img["data"]), "png_sha256": None, "not_white_pixels": _dark(dom_img),
                "coloured_pixels": pb.coloured_pixels(dom_img)},
        "canvas": {"box": cv, "sha256": _sha(cv_img["data"]), "png_sha256": None, "not_white_pixels": _dark(cv_img),
                   "coloured_pixels": pb.coloured_pixels(cv_img)},
        "identical": identical, "differing_pixels": diff["pixels"], "first_differing_pixel": diff["first"],
        "differing_box": diff["box"],
    }
    where = _evidence_dir(family)
    if where is not None:
        where.mkdir(parents=True, exist_ok=True)
        dom_png, cv_png = pb.encode_png(dom_img), pb.encode_png(cv_img)
        result["dom"]["png_sha256"], result["canvas"]["png_sha256"] = _sha(dom_png), _sha(cv_png)
        (where / "page.png").write_bytes(shot)
        (where / "dom.png").write_bytes(dom_png)
        (where / "canvas.png").write_bytes(cv_png)
        (where / "result.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # a box with no text in it would be equal to another box with no text in it
    assert result["dom"]["not_white_pixels"] > 0, "the DOM box of %s is blank: the screenshot has no text there" % family
    assert result["canvas"]["not_white_pixels"] > 0, "the canvas box of %s is blank: the SVG image drew no text" % family
    assert identical, (
        "%s: the text the screenshot rebuilt in the parent (the DOM box) is not the text the content drew (the canvas box): "
        "%d of %d pixels differ, the first at %r, inside the box %r. DOM sha256 %s, canvas sha256 %s. Not grey pixels: DOM %d, "
        "canvas %d (greyscale antialiasing has none: if they are not 0 the two boxes were antialiased in two ways)."
        % (family, diff["pixels"], BLOCK_W * BLOCK_H, diff["first"], diff["box"], result["dom"]["sha256"],
           result["canvas"]["sha256"], result["dom"]["coloured_pixels"], result["canvas"]["coloured_pixels"]))

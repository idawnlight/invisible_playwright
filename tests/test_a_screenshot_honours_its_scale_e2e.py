"""`screenshot(scale=...)` of a page, a full page, a clip and an element: CSS pixels for `"css"`, device pixels for `"device"` and for no `scale`.

Stock Playwright's Firefox sends `omitDeviceScaleFactor: scale === "css"` with the screenshot (`ffPage.takeScreenshot`, `Page.screenshot`). The driver of this
package always sent `False` and never read `scale`: at a viewport of 1280x720 and a device scale factor of 1.25 `page.screenshot(scale="css")` was 1600x900 where
stock gives 1280x720 (the existing `scale="css"` tests run at a ratio of 1, where the two cannot differ).

The size is not all: the content is read. The page holds colour blocks at known CSS positions (one in view, one below the fold, one in an `<iframe>`), and the pixel at the
position the scale puts them (x CSS pixels, or x times the ratio) has to be their colour. A picture of the right size taken from the wrong place, or scaled twice, is red.

The sizes asserted are the ones where ratio times size is a whole number (1.25 times a multiple of 4, 2 times anything): what the engine does with a fraction is its own
canvas rounding and nothing in the driver's bundle states it, so no case here depends on it.
"""
from __future__ import annotations

import pytest

import _page_bytes

pytestmark = pytest.mark.e2e

RED, GREEN, BLUE, PINK, WHITE = (204, 0, 0), (0, 170, 0), (0, 0, 204), (255, 0, 170), (255, 255, 255)

# CSS positions (left, top, width, height), all multiples of 4 so that 1.25 times each is whole.
RED_AT = (40, 20, 200, 100)
GREEN_AT = (400, 200, 200, 100)
BLUE_AT = (40, 1500, 200, 100)
FRAME_AT = (700, 40, 300, 200)
INNER_AT = (20, 30, 100, 60)          # inside the frame
VIEWPORT = (1280, 720)
PAGE_HEIGHT = 2000
CLIP = {"x": 380, "y": 180, "width": 240, "height": 140}    # holds the green block at (20, 20)

HTML = (
    "<!doctype html><html style='background:#fff'><body style='margin:0;background:#fff;height:%dpx'>"
    "<div id='red' style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;background:#cc0000'></div>"
    "<div id='green' style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;background:#00aa00'></div>"
    "<div id='blue' style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;background:#0000cc'></div>"
    "<iframe id='f' style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;border:0' srcdoc=\""
    "<body style='margin:0;background:#fff'>"
    "<div id='inner' style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;background:#ff00aa'></div>\"></iframe>"
    % ((PAGE_HEIGHT,) + RED_AT + GREEN_AT + BLUE_AT + FRAME_AT + INNER_AT)
)

RATIOS = (1.25, 2)
SCALES = ("css", "device", None)        # None: the argument is not given
KINDS = ("page", "full_page", "clip", "locator", "locator_below_the_fold", "locator_in_an_iframe")


def _read(png: bytes):
    image = _page_bytes.unfiltered(png)
    w, channels, data = image["w"], image["channels"], image["data"]

    def pixel(x: int, y: int):
        at = (y * w + x) * channels
        return tuple(data[at:at + 3])

    return image["w"], image["h"], pixel


def _near(a, b, tol=4):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def _kwargs(scale):
    return {} if scale is None else {"scale": scale}


@pytest.fixture(scope="module")
def shots(firefox_binary):
    """One browser, a context for each ratio, every photograph taken once: {(ratio, scale, kind): png bytes}, and the ratio each page reports."""
    from invisible_playwright import InvisiblePlaywright

    out, reported = {}, {}
    with InvisiblePlaywright(seed=4271, binary_path=firefox_binary, headless=True) as browser:
        for ratio in RATIOS:
            context = browser.new_context(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]}, device_scale_factor=ratio)
            page = context.new_page()
            page.set_content(HTML, wait_until="load")
            reported[ratio] = page.evaluate("window.devicePixelRatio")
            inner = page.frames[1].locator("#inner")
            inner.wait_for()
            for scale in SCALES:
                k = _kwargs(scale)
                out[(ratio, scale, "page")] = page.screenshot(**k)
                out[(ratio, scale, "full_page")] = page.screenshot(full_page=True, **k)
                out[(ratio, scale, "clip")] = page.screenshot(clip=CLIP, **k)
                out[(ratio, scale, "locator")] = page.locator("#red").screenshot(**k)
                out[(ratio, scale, "locator_below_the_fold")] = page.locator("#blue").screenshot(**k)
                out[(ratio, scale, "locator_in_an_iframe")] = inner.screenshot(**k)
            context.close()
    out["reported"] = reported
    return out


@pytest.mark.parametrize("ratio", RATIOS)
def test_the_context_has_the_ratio_it_was_given(shots, ratio):
    assert shots["reported"][ratio] == ratio


def _expected(kind):
    """(css size of the picture, [(css point in the picture, colour)])"""
    if kind == "page":
        return VIEWPORT, [
            ((RED_AT[0] + 100, RED_AT[1] + 50), RED),
            ((GREEN_AT[0] + 100, GREEN_AT[1] + 50), GREEN),
            ((FRAME_AT[0] + INNER_AT[0] + 50, FRAME_AT[1] + INNER_AT[1] + 30), PINK),
            ((10, 700), WHITE),
            ((600, 100), WHITE),
        ]
    if kind == "full_page":
        return (VIEWPORT[0], PAGE_HEIGHT), [
            ((RED_AT[0] + 100, RED_AT[1] + 50), RED),
            ((GREEN_AT[0] + 100, GREEN_AT[1] + 50), GREEN),
            ((BLUE_AT[0] + 100, BLUE_AT[1] + 50), BLUE),
            ((FRAME_AT[0] + INNER_AT[0] + 50, FRAME_AT[1] + INNER_AT[1] + 30), PINK),
            ((10, 1900), WHITE),
        ]
    if kind == "clip":
        return (CLIP["width"], CLIP["height"]), [
            ((GREEN_AT[0] - CLIP["x"] + 100, GREEN_AT[1] - CLIP["y"] + 50), GREEN),
            ((5, 5), WHITE),
            ((CLIP["width"] - 5, CLIP["height"] - 5), WHITE),
        ]
    boxes = {"locator": RED_AT, "locator_below_the_fold": BLUE_AT, "locator_in_an_iframe": INNER_AT}
    colour = {"locator": RED, "locator_below_the_fold": BLUE, "locator_in_an_iframe": PINK}[kind]
    w, h = boxes[kind][2], boxes[kind][3]
    return (w, h), [((w // 2, h // 2), colour), ((4, 4), colour), ((w - 5, h - 5), colour)]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("scale", SCALES, ids=["css", "device", "default"])
@pytest.mark.parametrize("ratio", RATIOS)
def test_the_picture_is_in_the_pixels_the_scale_asks_for_and_holds_the_page(shots, ratio, scale, kind):
    k = 1 if scale == "css" else ratio
    (css_w, css_h), points = _expected(kind)
    width, height, pixel = _read(shots[(ratio, scale, kind)])
    assert (width, height) == (round(css_w * k), round(css_h * k)), (
        "%s at ratio %s with scale=%r is %dx%d, not %dx%d (the CSS size %dx%d times %s)" % (
            kind, ratio, scale, width, height, round(css_w * k), round(css_h * k), css_w, css_h, k))
    for (x, y), colour in points:
        at = (round(x * k), round(y * k))
        assert _near(pixel(*at), colour), "%s at ratio %s with scale=%r: the pixel %s (CSS %s) is %s, not %s" % (
            kind, ratio, scale, at, (x, y), pixel(*at), colour)

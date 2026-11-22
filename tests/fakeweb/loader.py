"""One background load for the fake web: its own copy of the sites and a browser of ours that reloads an ad-heavy page every two
seconds until the process is stopped. `python loader.py BINARY [READY_FILE]` (READY_FILE is created once the first page has loaded or failed; run by tests/test_fakeweb_p2_e2e.py, three at a time)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fakeweb import sites  # noqa: E402

from invisible_playwright import InvisiblePlaywright  # noqa: E402

origin, _other, stop = sites.start((0, 0))
try:
    with InvisiblePlaywright(binary_path=sys.argv[1], headless=True, seed=7) as browser:
        page = browser.new_context().new_page()
        announced = False
        while True:
            try:
                page.goto(origin + "/p/ad_iframes", wait_until="load", timeout=15000)
            except Exception:
                pass
            if not announced and len(sys.argv) > 2:
                open(sys.argv[2], "w").close()
                announced = True
            time.sleep(2)
finally:
    stop()

"""Drive the UI in headless Chromium and take screenshots (dev tool).

Usage: python -m tools.e2e_shot file.pdf "start of block text" out_prefix [new text to append]
"""

import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"


def main() -> None:
    pdf, prefix, out = sys.argv[1], sys.argv[2], sys.argv[3]
    append = sys.argv[4] if len(sys.argv) > 4 else None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=2)
        logs = []
        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
        page.goto(BASE)
        page.screenshot(path=f"{out}_home.png")
        page.set_input_files("input[type=file]", pdf)
        page.wait_for_selector(".page-img", timeout=20000)
        page.wait_for_selector(".block-outline", timeout=20000)
        time.sleep(0.8)
        page.screenshot(path=f"{out}_doc.png")
        # Find the block whose text starts with prefix via the API model.
        doc_id = page.url.rstrip("/").split("/")[-1]
        model = page.evaluate(f"fetch('/api/documents/{doc_id}/pages/0/text').then(r => r.json())")
        block = next(b for b in model["blocks"] if "".join(r["text"] for r in b["runs"]).startswith(prefix))
        idx = [b["id"] for b in model["blocks"] if b["id"] == block["id"]]
        outlines = page.locator(".page[data-page='0'] .block-outline")
        target = None
        for i in range(outlines.count()):
            el = outlines.nth(i)
            box = el.bounding_box()
            # Match by geometry: compare with the block bbox scaled.
            target = target or None
        # Click in the middle of the first line of the block.
        zoom = page.evaluate("Number(localStorage.getItem('plume.zoom')) || 1.25")
        page_box = page.locator(".page[data-page='0']").bounding_box()
        line = block["lines"][0]
        cx = page_box["x"] + (line["bbox"][0] + 3) * zoom
        cy = page_box["y"] + (line["bbox"][1] + line["bbox"][3]) / 2 * zoom
        clip = {
            "x": page_box["x"] + block["bbox"][0] * zoom - 30,
            "y": page_box["y"] + block["bbox"][1] * zoom - 20,
            "width": (block["bbox"][2] - block["bbox"][0]) * zoom + 60,
            "height": (block["bbox"][3] - block["bbox"][1]) * zoom + 40,
        }
        page.mouse.move(cx, cy)
        time.sleep(0.2)
        page.screenshot(path=f"{out}_before.png", clip=clip)
        page.mouse.click(cx, cy)
        page.wait_for_selector(".block-text", timeout=5000)
        time.sleep(1.2)
        page.screenshot(path=f"{out}_editing.png", clip=clip)
        page.screenshot(path=f"{out}_editing_full.png")
        if append:
            page.keyboard.press("End")
            page.keyboard.type(append)
            time.sleep(0.4)
            page.screenshot(path=f"{out}_typed.png", clip={**clip, "height": clip["height"] + 40})
            page.click("button:has-text('Valider')")
            time.sleep(2.5)
            page.screenshot(path=f"{out}_after.png", clip={**clip, "height": clip["height"] + 40})
        print("\n".join(logs[-20:]))
        browser.close()


if __name__ == "__main__":
    main()

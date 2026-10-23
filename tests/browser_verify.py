"""Opt-in Chrome UI checks; uses existing Chrome, no browser download."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--url",default="http://100.122.59.107:18080")
    args=parser.parse_args()
    output=Path("verification");output.mkdir(exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel="chrome",headless=True,args=["--no-proxy-server"])
        page=browser.new_page(viewport={"width":1440,"height":1100},device_scale_factor=1)
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.goto(args.url,wait_until="networkidle")
        expect(page.locator("#cpu")).not_to_have_text("—")
        page.screenshot(path=str(output/"dashboard.png"),full_page=True)
        assert page.locator("#health").inner_text()=="监控在线",page.locator("#banner").inner_text()
        page.get_by_role("button",name="请求记录",exact=True).click()
        page.wait_for_selector("tr[data-id]")
        page.locator("tr[data-id]").first.click()
        page.wait_for_selector("dialog[open]")
        assert "known_options" in page.locator("#detail-json").inner_text()
        page.screenshot(path=str(output/"request-detail.png"),full_page=True)
        page.get_by_role("button",name="关闭详情").click()
        page.get_by_role("button",name="模型对比",exact=True).click()
        expect(page.locator("#summary-rows")).to_contain_text("qwen")
        page.screenshot(path=str(output/"model-comparison.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        page.get_by_role("button",name="系统总览",exact=True).click()
        page.wait_for_timeout(300)
        page.screenshot(path=str(output/"mobile.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),"Page overflows mobile viewport"
        assert not errors,errors
        report={"passed":True,"javascript_errors":errors,"screenshots":["dashboard.png","request-detail.png","model-comparison.png","mobile.png"]}
        (output/"browser-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report));browser.close()

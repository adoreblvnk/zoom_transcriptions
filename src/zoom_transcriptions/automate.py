"""Automate Zoom transcription downloads from D2L Brightspace."""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ZOOM_API_HOST = "applications.zoom.us"


def load_config():
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return {}


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name.strip("_")


async def login_to_d2l(page, email: str, password: str, base_url: str):
    login_url = f"{base_url}/d2l/login"
    print(f"🔐 Navigating to D2L login: {login_url}")
    await page.goto(login_url, wait_until="networkidle")

    sit_login = page.locator("#samlLinkId")
    if await sit_login.count() > 0:
        print("  🔗 Clicking SIT Login button...")
        await sit_login.click()
        await page.wait_for_load_state("networkidle")

    await page.fill("#userNameInput", email)
    await page.fill("#passwordInput", password)
    await page.click("#submitButton")
    await page.wait_for_load_state("networkidle")
    print("✅ D2L login successful")


def find_zoom_frame(page):
    for frame in page.frames:
        if ZOOM_API_HOST in frame.url:
            return frame
    return None


async def load_recordings(page, url: str):
    """Navigate to Zoom LTI URL, click Cloud Recordings, return list of recordings."""
    lti_scid = None
    recordings = []

    async def on_response(response):
        nonlocal lti_scid, recordings
        if ZOOM_API_HOST not in response.url:
            return
        if "lti_scid" in response.url:
            parsed = __import__("urllib.parse", fromlist=["urlparse", "parse_qs"]).urlparse(response.url)
            params = __import__("urllib.parse", fromlist=["urlparse", "parse_qs"]).parse_qs(parsed.query)
            if "lti_scid" in params:
                lti_scid = params["lti_scid"][0]
        if "/COURSE" in response.url:
            try:
                data = await response.json()
                recordings = data.get("result", {}).get("list", [])
            except (json.JSONDecodeError, KeyError):
                pass

    page.on("response", on_response)
    await page.goto(url, wait_until="networkidle")
    await asyncio.sleep(5)
    page.remove_listener("response", on_response)

    if not lti_scid:
        return [], None

    zoom_frame = find_zoom_frame(page)
    if not zoom_frame:
        return [], None

    try:
        all_tabs = zoom_frame.locator(".ant-tabs-tab")
        tab_count = await all_tabs.count()
        for t in range(tab_count):
            text = await all_tabs.nth(t).inner_text()
            if "cloud" in text.lower():
                recordings = []
                page.on("response", on_response)
                await all_tabs.nth(t).click()
                await asyncio.sleep(5)
                page.remove_listener("response", on_response)
                break
    except (AttributeError, TypeError):
        pass

    return recordings, lti_scid


async def get_recording_details(page, row_key: int) -> tuple[str, str]:
    """Click a recording link, intercept file+pwd responses, return (play_url, password)."""
    play_url = ""
    password = ""

    async def on_response(response):
        nonlocal play_url, password
        if ZOOM_API_HOST not in response.url:
            return
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            data = await response.json()
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
            return
        if "/pwd" in response.url:
            pw = data.get("result", {}).get("password", "")
            if pw:
                password = pw
        elif "/file" in response.url:
            for f in data.get("result", {}).get("recordingFiles", []):
                if f.get("fileType") == "MP4" and f.get("playUrl"):
                    play_url = f["playUrl"]
                    break

    zoom_frame = find_zoom_frame(page)
    if not zoom_frame:
        return "", ""

    page.on("response", on_response)

    clicked = await zoom_frame.evaluate(f"""
        () => {{
            const spans = Array.from(document.querySelectorAll('span[role="button"]'))
                .filter(s => s.textContent.trim().length > 0);
            if ({row_key} >= spans.length) return 'out-of-range: ' + spans.length;
            spans[{row_key}].click();
            return 'clicked: ' + spans[{row_key}].textContent.trim();
        }}
    """)
    print(f"    📋 List click: {clicked}")

    if "out-of-range" in clicked or "not-found" in clicked:
        page.remove_listener("response", on_response)
        return "", ""

    await asyncio.sleep(5)

    clicked_play = await zoom_frame.evaluate("""
        () => {
            const playBtns = document.querySelectorAll('.lti-recording-item-play-media');
            if (playBtns.length === 0) return 'no-play-btns';
            for (let i = 0; i < playBtns.length; i++) {
                const icon = playBtns[i].querySelector('i[aria-label]');
                const label = icon ? icon.getAttribute('aria-label') : '';
                if (label.includes('Recording') && !label.includes('Audio')) {
                    playBtns[i].click();
                    return 'clicked-video: ' + label;
                }
            }
            playBtns[0].click();
            return 'clicked-first: ' + playBtns.length;
        }
    """)
    print(f"    🖱️  Play click: {clicked_play}")

    await asyncio.sleep(5)
    page.remove_listener("response", on_response)

    for p in page.context.pages:
        if p != page:
            await p.close()

    return play_url, password


async def process_module(page, module_code, module_url, output_dir, skip_existing):
    print(f"\n{'='*60}")
    print(f"📚 Module: {module_code}")
    print(f"{'='*60}")

    module_dir = output_dir / module_code
    module_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    skipped = 0
    failed = 0

    recordings, _lti_scid = await load_recordings(page, module_url)
    if not recordings:
        print("  ❌ No recordings found")
        return 0, 0, 1

    print(f"  📋 Found {len(recordings)} recording(s)")

    for i, recording in enumerate(recordings):
        topic = recording.get("topic", "Unknown Recording")
        start_time = recording.get("startTime", "")
        date_str = start_time.split(" ")[0] if start_time else "unknown_date"
        filename = sanitize_filename(f"{topic}_{date_str}")
        output_path = module_dir / f"{filename}.md"

        if skip_existing and output_path.exists():
            print(f"  ⏭️  Skipping '{filename}' (already exists)")
            skipped += 1
            continue

        print(f"  📥 Getting details for: {topic} ({start_time})")

        play_url = ""
        rec_password = ""
        for attempt in range(3):
            play_url, rec_password = await get_recording_details(page, i)
            if play_url and rec_password:
                break
            if attempt < 2:
                print(f"    🔄 Retrying (attempt {attempt + 2})...")
                await asyncio.sleep(2)
                await page.goto(module_url, wait_until="networkidle")
                await asyncio.sleep(3)

                zoom_frame = find_zoom_frame(page)
                if zoom_frame:
                    try:
                        all_tabs = zoom_frame.locator(".ant-tabs-tab")
                        tab_count = await all_tabs.count()
                        for t in range(tab_count):
                            text = await all_tabs.nth(t).inner_text()
                            if "cloud" in text.lower():
                                await all_tabs.nth(t).click()
                                await asyncio.sleep(3)
                                break
                    except (AttributeError, TypeError):
                        pass

        if not play_url or not rec_password:
            print(f"  ⚠️  Could not get details for '{topic}'")
            failed += 1
            await page.goto(module_url, wait_until="networkidle")
            await asyncio.sleep(3)

            zoom_frame = find_zoom_frame(page)
            if zoom_frame:
                try:
                    all_tabs = zoom_frame.locator(".ant-tabs-tab")
                    tab_count = await all_tabs.count()
                    for t in range(tab_count):
                        text = await all_tabs.nth(t).inner_text()
                        if "cloud" in text.lower():
                            await all_tabs.nth(t).click()
                            await asyncio.sleep(3)
                            break
                except (AttributeError, TypeError):
                    pass
            continue

        print(f"  📥 Downloading transcript for: {topic}")

        try:
            from .zoom import fetch_zoom_recording
            fetch_zoom_recording(play_url, rec_password, str(output_path))
            downloaded += 1
        except SystemExit as e:
            print(f"  ❌ Failed to download '{topic}': {e}")
            failed += 1

        await asyncio.sleep(1)
        await page.goto(module_url, wait_until="networkidle")
        await asyncio.sleep(3)

        zoom_frame = find_zoom_frame(page)
        if zoom_frame:
            try:
                all_tabs = zoom_frame.locator(".ant-tabs-tab")
                tab_count = await all_tabs.count()
                for t in range(tab_count):
                    text = await all_tabs.nth(t).inner_text()
                    if "cloud" in text.lower():
                        await all_tabs.nth(t).click()
                        await asyncio.sleep(3)
                        break
            except (AttributeError, TypeError):
                pass

    return downloaded, skipped, failed


async def run(args):
    load_dotenv()

    email = args.email or os.getenv("D2L_EMAIL")
    password = args.password or os.getenv("D2L_PASSWORD")
    base_url = args.base_url or os.getenv("D2L_BASE_URL", "https://xsite.singaporetech.edu.sg")

    if not email or not password:
        print("❌ D2L credentials required.")
        print("   Use --email/--password flags or set D2L_EMAIL/D2L_PASSWORD in .env")
        sys.exit(1)

    config = load_config()
    modules = config.get("modules", {})

    if not modules:
        print("❌ No modules configured.")
        sys.exit(1)

    if args.module:
        if args.module in modules:
            modules = {args.module: modules[args.module]}
        else:
            print(f"❌ Unknown module: {args.module}")
            sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir != "transcriptions" else Path(config.get("output_dir", "transcriptions"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        context = await browser.new_context()
        page = await context.new_page()

        print("🔐 Logging into D2L...")
        await login_to_d2l(page, email, password, base_url)

        total_downloaded = 0
        total_skipped = 0
        total_failed = 0

        for module_code, module_config in modules.items():
            module_url = module_config.get("url", "")
            if not module_url:
                print(f"❌ No URL for module: {module_code}")
                continue

            d, s, f = await process_module(
                page, module_code, module_url, output_dir, args.skip_existing
            )
            total_downloaded += d
            total_skipped += s
            total_failed += f

        await browser.close()

        print(f"\n{'='*60}")
        print("📊 Summary")
        print(f"{'='*60}")
        print(f"  ✅ Downloaded: {total_downloaded}")
        print(f"  ⏭️  Skipped:    {total_skipped}")
        print(f"  ❌ Failed:     {total_failed}")


def main():
    parser = argparse.ArgumentParser(
        description="Automate Zoom transcription downloads from D2L"
    )
    parser.add_argument("--email", help="D2L email address")
    parser.add_argument("--password", help="D2L password")
    parser.add_argument("--base-url", help="D2L base URL")
    parser.add_argument("--module", help="Process specific module only")
    parser.add_argument("--output-dir", default="transcriptions", help="Output directory")
    parser.add_argument("--skip-existing", action="store_true", default=True)
    parser.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

"""Fetch Zoom recording transcripts using an existing Playwright Chromium page."""

import urllib.parse

import webvtt
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


def parse_vtt_to_markdown(vtt_text: str, title: str = "") -> str:
    """Parse WebVTT content into Markdown with timestamps and speakers."""
    md_lines = [f"# {title}\n"] if title else []
    for caption in webvtt.from_string(vtt_text):
        start_ts = caption.start.split(".")[0]
        text = caption.text.strip()
        if not text:
            continue
        if ":" in text and not text.startswith("http"):
            speaker, content = text.split(":", 1)
            md_lines.append(f"[{start_ts}] **{speaker.strip()}**: {content.strip()}")
        else:
            md_lines.append(f"[{start_ts}] {text}")
    return "\n\n".join(md_lines)


async def _handle_passcode(page: Page, password: str, headed: bool) -> None:
    """Submit the Zoom recording passcode, pausing for manual CAPTCHA when headed."""
    passcode_input = page.locator('input[type="password"]')
    enabled_btn = page.locator(
        "button.passcode-submit:not(.is-primary-disabled):not([disabled])"
    )
    captcha_locator = page.locator(
        'img[src*="captcha" i], iframe[src*="captcha" i], '
        'iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i]'
    )

    await passcode_input.fill(password)

    needs_captcha = (
        await captcha_locator.count() > 0
        and await captcha_locator.first.is_visible()
    )
    if not needs_captcha:
        try:
            await enabled_btn.wait_for(state="visible", timeout=2_000)
        except PlaywrightTimeoutError:
            needs_captcha = True

    if not needs_captcha:
        await enabled_btn.click()
        try:
            await passcode_input.wait_for(state="detached", timeout=10_000)
        except PlaywrightTimeoutError:
            needs_captcha = (
                await captcha_locator.count() > 0
                and await captcha_locator.first.is_visible()
            ) or await enabled_btn.count() == 0
            if not needs_captcha:
                err = page.locator('[role="alert"], .zoom-form-item__error')
                msg = (await err.first.inner_text()).strip() if await err.count() > 0 else ""
                raise RuntimeError(
                    f"Zoom passcode validation failed: {msg or 'timed out waiting for player'}"
                )

    if needs_captcha:
        if not headed:
            raise RuntimeError(
                "Zoom requires a CAPTCHA. Re-run with --headed to solve it once in Chromium."
            )
        print("    🧩 Zoom requires a CAPTCHA — solve it in Chromium and click Confirm...")
        await passcode_input.wait_for(state="detached", timeout=180_000)

    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_function("() => Boolean(window.__zm_unified_player__)")


async def fetch_zoom_recording(
    page: Page, url: str, password: str, *, headed: bool = False
) -> str:
    """Load a Zoom recording in Playwright and return its transcript as Markdown."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Invalid Zoom URL: {url}")

    print(f"🔗 Connecting to Zoom recording: {url}")
    if parsed.path not in page.url:
        await page.goto(url, wait_until="domcontentloaded")

    await page.wait_for_function("() => Boolean(window.__zm_unified_player__)")
    await page.wait_for_function(
        "() => Boolean(document.querySelector('input[type=\"password\"], video'))"
    )

    passcode_input = page.locator('input[type="password"]')
    if await passcode_input.count() > 0 and await passcode_input.first.is_visible():
        print("🔑 Validating Zoom password...")
        await _handle_passcode(page, password, headed=headed)

    cfg = await page.evaluate("() => window.__zm_unified_player__")
    api_base = cfg.get("apiDomain") or f"{parsed.scheme}://{parsed.netloc}/"
    play_api = cfg.get("playInfoApi")
    feature_api = cfg.get("featureConfigApi")
    if not play_api or not feature_api:
        raise RuntimeError("Missing Zoom unified player API endpoints.")

    req = page.context.request
    info_resp = await req.get(urllib.parse.urljoin(api_base, play_api))
    info_json = await info_resp.json()
    if not info_json.get("status"):
        raise RuntimeError(
            f"Could not read Zoom recording info: {info_json.get('errorMessage') or info_json.get('errorCode')}"
        )
    topic = (
        ((info_json.get("result") or {}).get("mediaInfo") or {}).get("mediaTopic")
        or "Zoom Recording"
    )
    print(f"📌 Recording Topic: {topic}")

    feat_resp = await req.get(urllib.parse.urljoin(api_base, feature_api))
    feat_json = await feat_resp.json()
    if not feat_json.get("status"):
        raise RuntimeError(
            f"Could not read Zoom feature config: {feat_json.get('errorMessage') or feat_json.get('errorCode')}"
        )
    features = feat_json.get("result") or {}

    captions = features.get("captions") or {}
    vtt_api = captions.get("transcriptCaptionApi") or captions.get("closedCaptionApi")
    if vtt_api:
        vtt_resp = await req.get(urllib.parse.urljoin(api_base, vtt_api))
        vtt_text = await vtt_resp.text()
        if vtt_resp.ok and vtt_text.lstrip().startswith("WEBVTT"):
            return parse_vtt_to_markdown(vtt_text, title=topic)

    transcript = features.get("transcript") or {}
    if transcript.get("status") == "success" and transcript.get("getApi"):
        t_resp = await req.get(urllib.parse.urljoin(api_base, transcript["getApi"]))
        t_json = await t_resp.json()
        items = (t_json.get("result") or {}).get("dataList") or []
        if items:
            lines = [f"# {topic}\n"]
            for item in items:
                text = (item.get("text") or "").strip()
                if not text:
                    continue
                start = (item.get("startTime") or "").split(".")[0]
                speaker = (item.get("userName") or "").strip()
                lines.append(
                    f"[{start}] **{speaker}**: {text}" if speaker else f"[{start}] {text}"
                )
            return "\n\n".join(lines)

    raise RuntimeError("No transcript or captions available for this recording.")

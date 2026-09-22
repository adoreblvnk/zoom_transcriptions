# /// script
# requires-python = ">=3.12"
# ///

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import requests
import webvtt
from tqdm import tqdm


def parse_vtt_to_markdown(vtt_text: str, title: str = "") -> str:
    """Parses WebVTT content into clean Markdown with timestamps and speakers."""
    md_lines = [f"# {title}\n"] if title else []
    for caption in webvtt.from_string(vtt_text):
        start_ts = caption.start.split(".")[0]  # "HH:MM:SS.mmm" → "HH:MM:SS"
        text = caption.text.strip()
        if not text:
            continue
        if ":" in text and not text.startswith("http"):
            speaker, content = text.split(":", 1)
            md_lines.append(f"[{start_ts}] **{speaker.strip()}**: {content.strip()}")
        else:
            md_lines.append(f"[{start_ts}] {text}")
    return "\n\n".join(md_lines)


def fetch_zoom_recording(url: str, password: str, output_md: str | None = "transcript.md") -> str:
    """Fetches Zoom transcript or downloads video and runs local transcription script."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        sys.exit(f"❌ Invalid Zoom URL: {url}")

    base_url = f"{parsed.scheme}://{parsed.netloc}/"

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": url,
    })

    print(f"🔗 Connecting to Zoom recording: {url}")

    # 1. Fetch initial webpage
    try:
        html = session.get(url).text
    except Exception as e:
        sys.exit(f"❌ Failed to reach Zoom URL: {e}")

    file_id_match = re.search(r"fileId\s*:\s*[\'\"]([^\'\"]*)[\'\"]", html)
    meeting_id_match = re.search(r"meetingId\s*:\s*[\'\"]([^\'\"]*)[\'\"]", html)

    file_id = file_id_match.group(1) if file_id_match else None
    meeting_id = meeting_id_match.group(1) if meeting_id_match else None

    # Handle share URL or missing fileId
    if "/rec/share/" in url or (meeting_id and not file_id):
        if meeting_id:
            val_url = base_url + "rec/validate_passwd"
            session.post(val_url, data={"id": meeting_id, "passwd": password, "action": "share"})

            try:
                s_json = session.get(f"{base_url}nws/recording/1.0/play/share-info/{meeting_id}").json()
                redirect_path = s_json.get("result", {}).get("redirectUrl")
                if redirect_path:
                    html = session.get(urllib.parse.urljoin(base_url, redirect_path)).text
                    file_id_match = re.search(r"fileId\s*:\s*[\'\"]([^\'\"]*)[\'\"]", html)
                    if file_id_match:
                        file_id = file_id_match.group(1)
            except Exception as e:
                print(f"⚠️ Warning during share info lookup: {e}")

    if not file_id:
        sys.exit("❌ Could not extract recording file ID from Zoom page.")

    # 2. Validate password
    print("🔑 Validating Zoom password...")
    try:
        val_res = session.post(
            base_url + "rec/validate_passwd",
            data={"id": file_id, "passwd": password, "action": "play"},
        ).json()
        if not val_res.get("status"):
            sys.exit(f"❌ Zoom passcode validation failed: {val_res.get('errorMessage') or 'Invalid password'}")
    except Exception as e:
        sys.exit(f"❌ Error during passcode validation: {e}")

    # 3. Fetch play info
    try:
        info_data = session.get(f"{base_url}nws/recording/1.0/play/info/{file_id}").json().get("result", {})
    except Exception as e:
        sys.exit(f"❌ Failed to fetch recording info: {e}")

    topic = info_data.get("meet", {}).get("topic", "Zoom Recording")
    print(f"📌 Recording Topic: {topic}")

    # 4. Check for transcript or subtitles
    t_url = info_data.get("transcriptUrl") or info_data.get("ccUrl")
    if t_url:
        print("📄 Subtitles/transcript found! Downloading...")
        try:
            vtt_text = session.get(urllib.parse.urljoin(base_url, t_url)).text
            md_content = parse_vtt_to_markdown(vtt_text, title=topic)
            if output_md:
                Path(output_md).write_text(md_content, encoding="utf-8")
                print(f"✅ Transcript saved successfully to {output_md}")
            return md_content
        except Exception as e:
            print(f"⚠️ Failed to download transcript file: {e}. Falling back to video download...")

    # 5. Fallback: Download video and invoke main.py
    mp4_url = info_data.get("viewMp4Url") or info_data.get("mp4Url") or info_data.get("shareMp4Url")
    if not mp4_url:
        sys.exit("❌ Subtitles not available and no MP4 download URL found.")

    print("🎥 Subtitles not available on Zoom. Downloading video file...")
    video_filename = "downloaded_recording.mp4"
    try:
        resp = session.get(mp4_url, stream=True)
        total = int(resp.headers.get("Content-Length", 0))
        with open(video_filename, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True, unit_divisor=1024, desc="📥 Downloading"
        ) as bar:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                bar.update(len(chunk))
        print(f"✅ Video downloaded: {video_filename}")
    except Exception as e:
        sys.exit(f"❌ Video download failed: {e}")

    # Invoke main.py for transcription
    print("🎙️ Passing downloaded video to main.py for local transcription...")
    main_script = Path(__file__).parent / "main.py"
    if not main_script.exists():
        sys.exit("❌ Error: main.py script not found for local video transcription.")

    result = subprocess.run(["uv", "run", str(main_script), video_filename, output_md])
    if result.returncode != 0:
        sys.exit(f"❌ Transcription via main.py failed with exit code {result.returncode}")
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Zoom recording transcripts into transcript.md (downloads video and runs main.py if subtitles unavailable)."
    )
    parser.add_argument("args", nargs="*", help="[password] [url] OR [url] [password]")
    parser.add_argument("-u", "--url", help="Zoom recording URL")
    parser.add_argument("-p", "--password", help="Zoom recording password")
    parser.add_argument("-o", "--output", default="transcript.md", help="Output markdown path (default: transcript.md)")

    parsed_args = parser.parse_args()

    url = parsed_args.url
    password = parsed_args.password

    for p in parsed_args.args:
        if "zoom.us" in p or p.startswith("http://") or p.startswith("https://"):
            url = p
        else:
            password = p

    if not password:
        try:
            password = input("🔑 Enter Zoom password: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit("\nOperation cancelled.")

    if not url:
        try:
            url = input("🔗 Enter Zoom recording link: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit("\nOperation cancelled.")

    if not password:
        sys.exit("❌ Password cannot be empty.")
    if not url:
        sys.exit("❌ Zoom recording link cannot be empty.")

    fetch_zoom_recording(url, password, output_md=parsed_args.output)


if __name__ == "__main__":
    main()

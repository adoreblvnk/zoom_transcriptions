# /// script
# requires-python = ">=3.12"
# ///

import argparse
import http.cookiejar
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def parse_vtt_to_markdown(vtt_text: str, title: str = "") -> str:
    """Parses WebVTT content into clean Markdown with timestamps and speakers."""
    lines = vtt_text.strip().splitlines()
    md_lines = []
    if title:
        md_lines.append(f"# {title}\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            raw_start = line.split("-->")[0].strip()
            start_ts = raw_start.split(".")[0]  # HH:MM:SS

            i += 1
            text_lines = []
            while (
                i < len(lines)
                and lines[i].strip()
                and not lines[i].strip().isdigit()
                and "-->" not in lines[i]
            ):
                text_lines.append(lines[i].strip())
                i += 1

            full_text = " ".join(text_lines)
            if full_text:
                if ":" in full_text and not full_text.startswith("http"):
                    parts = full_text.split(":", 1)
                    speaker = parts[0].strip()
                    content = parts[1].strip()
                    md_lines.append(f"[{start_ts}] **{speaker}**: {content}")
                else:
                    md_lines.append(f"[{start_ts}] {full_text}")
            continue
        i += 1

    return "\n\n".join(md_lines)


def fetch_zoom_recording_text(url: str, password: str) -> str:
    """Like fetch_zoom_recording but returns the markdown string instead of writing to disk."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
    tmp.close()
    tmp_path = tmp.name
    try:
        fetch_zoom_recording(url, password, output_md=tmp_path)
        return Path(tmp_path).read_text(encoding="utf-8")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def fetch_zoom_recording(url: str, password: str, output_md: str = "transcript.md"):
    """Fetches Zoom transcript or downloads video and runs local transcription script."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        sys.exit(f"❌ Invalid Zoom URL: {url}")

    base_url = f"{parsed.scheme}://{parsed.netloc}/"

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        (
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ),
        ("Referer", url),
    ]

    print(f"🔗 Connecting to Zoom recording: {url}")

    # 1. Fetch initial webpage
    try:
        req = urllib.request.Request(url)
        with opener.open(req) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
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
            data = urllib.parse.urlencode(
                {"id": meeting_id, "passwd": password, "action": "share"}
            ).encode("utf-8")
            req = urllib.request.Request(
                val_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            try:
                with opener.open(req) as resp:
                    pass
            except Exception:
                pass

            share_info_url = f"{base_url}nws/recording/1.0/play/share-info/{meeting_id}"
            req = urllib.request.Request(share_info_url)
            try:
                with opener.open(req) as resp:
                    s_json = json.loads(resp.read().decode("utf-8"))
                    redirect_path = s_json.get("result", {}).get("redirectUrl")
                    if redirect_path:
                        redirect_url = urllib.parse.urljoin(base_url, redirect_path)
                        req = urllib.request.Request(redirect_url)
                        with opener.open(req) as resp:
                            html = resp.read().decode("utf-8", errors="ignore")
                        file_id_match = re.search(r"fileId\s*:\s*[\'\"]([^\'\"]*)[\'\"]", html)
                        if file_id_match:
                            file_id = file_id_match.group(1)
            except Exception as e:
                print(f"⚠️ Warning during share info lookup: {e}")

    if not file_id:
        sys.exit("❌ Could not extract recording file ID from Zoom page.")

    # 2. Validate password
    print("🔑 Validating Zoom password...")
    val_url = base_url + "rec/validate_passwd"
    data = urllib.parse.urlencode({"id": file_id, "passwd": password, "action": "play"}).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        val_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with opener.open(req) as resp:
            val_res = json.loads(resp.read().decode("utf-8"))
            if not val_res.get("status"):
                err_msg = val_res.get("errorMessage") or "Invalid password"
                sys.exit(f"❌ Zoom passcode validation failed: {err_msg}")
    except Exception as e:
        sys.exit(f"❌ Error during passcode validation: {e}")

    # 3. Fetch play info
    info_url = f"{base_url}nws/recording/1.0/play/info/{file_id}"
    req = urllib.request.Request(info_url)
    try:
        with opener.open(req) as resp:
            info_data = json.loads(resp.read().decode("utf-8")).get("result", {})
    except Exception as e:
        sys.exit(f"❌ Failed to fetch recording info: {e}")

    topic = info_data.get("meet", {}).get("topic", "Zoom Recording")
    print(f"📌 Recording Topic: {topic}")

    # 4. Check for transcript or subtitles
    t_url = info_data.get("transcriptUrl") or info_data.get("ccUrl")
    if t_url:
        print("📄 Subtitles/transcript found! Downloading...")
        full_t_url = urllib.parse.urljoin(base_url, t_url)
        try:
            req = urllib.request.Request(full_t_url)
            with opener.open(req) as resp:
                vtt_text = resp.read().decode("utf-8")
                md_content = parse_vtt_to_markdown(vtt_text, title=topic)
                Path(output_md).write_text(md_content, encoding="utf-8")
                print(f"✅ Transcript saved successfully to {output_md}")
                return
        except Exception as e:
            print(f"⚠️ Failed to download transcript file: {e}. Falling back to video download...")

    # 5. Fallback: Download video and invoke main.py
    mp4_url = (
        info_data.get("viewMp4Url")
        or info_data.get("mp4Url")
        or info_data.get("shareMp4Url")
    )
    if not mp4_url:
        sys.exit("❌ Subtitles not available and no MP4 download URL found.")

    print("🎥 Subtitles not available on Zoom. Downloading video file...")
    video_filename = "downloaded_recording.mp4"
    try:
        req = urllib.request.Request(mp4_url)
        with opener.open(req) as resp, open(video_filename, "wb") as out_file:
            total_length = resp.headers.get("Content-Length")
            total = int(total_length) if total_length and total_length.isdigit() else 0
            downloaded = 0
            block_size = 1024 * 1024  # 1MB blocks

            while True:
                buffer = resp.read(block_size)
                if not buffer:
                    break
                downloaded += len(buffer)
                out_file.write(buffer)
                if total > 0:
                    percent = (downloaded / total) * 100
                    print(
                        f"\r📥 Downloaded {downloaded / (1024*1024):.1f} MB / {total / (1024*1024):.1f} MB ({percent:.1f}%)",
                        end="",
                        flush=True,
                    )
                else:
                    print(f"\r📥 Downloaded {downloaded / (1024*1024):.1f} MB", end="", flush=True)

            print(f"\n✅ Video downloaded: {video_filename}")
    except Exception as e:
        sys.exit(f"❌ Video download failed: {e}")

    # Invoke main.py for transcription
    print("🎙️ Passing downloaded video to main.py for local transcription...")
    script_dir = Path(__file__).parent
    main_script = script_dir / "main.py"

    if not main_script.exists():
        sys.exit("❌ Error: main.py script not found for local video transcription.")

    cmd = ["uv", "run", str(main_script), video_filename, output_md]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"❌ Transcription via main.py failed with exit code {result.returncode}")


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
    positional = parsed_args.args

    if positional:
        for p in positional:
            if "zoom.us" in p or p.startswith("http://") or p.startswith("https://"):
                url = p
            else:
                password = p

    # Prompt interactively if missing: password first, then link
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

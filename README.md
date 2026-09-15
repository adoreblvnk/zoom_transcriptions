# Zoom Transcriptions

Automate downloading Zoom transcription recordings from D2L Brightspace.

## Setup

1. Install dependencies:

```bash
uv sync
playwright install chromium
```

2. Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp src/zoom_transcriptions/.env.example src/zoom_transcriptions/.env
```

3. Copy `config.example.json` to `config.json` and configure your modules:

```bash
cp src/zoom_transcriptions/config.example.json src/zoom_transcriptions/config.json
```

## Configuration

### .env file

```
D2L_EMAIL=your_email@singaporetech.edu.sg
D2L_PASSWORD=your_password
```

### config.json file

```json
{
  "output_dir": "transcriptions",
  "modules": {
    "INF2004": {
      "url": "https://xsite.singaporetech.edu.sg/d2l/common/dialogs/quickLink/quickLink.d2l?ou=YOUR_OU&type=lti&rcode=YOUR_RCODE&srcou=YOUR_SRCOU&launchFramed=1&framedName=Zoom+Meeting"
    }
  }
}
```

To find the URL for your module:

1. Go to your D2L course page
2. Navigate to Communications > Zoom Meeting
3. Copy the URL from your browser's address bar

## Usage

Run for all configured modules:

```bash
uv run python -m zoom_transcriptions.automate
```

Run for a specific module:

```bash
uv run python -m zoom_transcriptions.automate --module INF2004
```

Run in headed mode (visible browser):

```bash
uv run python -m zoom_transcriptions.automate --headed
```

Skip re-downloading existing transcripts (default behaviour):

```bash
uv run python -m zoom_transcriptions.automate --skip-existing
```

Re-download all transcripts:

```bash
uv run python -m zoom_transcriptions.automate --no-skip-existing
```

## How it works

1. Logs into D2L via ADFS SAML authentication
2. Navigates to the Zoom LTI page for each module
3. Clicks the "Cloud Recordings" tab
4. For each recording:
   - Clicks the recording link to open details
   - Clicks the video play button
   - Intercepts the Zoom API responses to get the play URL and password
   - Downloads the transcript using the existing `fetch_zoom_recording` function
5. Saves transcripts as Markdown files in the configured output directory

## Output

Transcripts are saved as:

```
transcriptions/
  INF2004/
    INF2004_LECTURE_2026-09-14.md
    INF2004_LECTURE_2026-09-07.md
    Group_Project_Briefing_2026-08-31.md
```

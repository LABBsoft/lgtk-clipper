# lgtk-clipper

Post-stream helper: point it at a Twitch VOD and it ranks the viewer-made
clips most likely to work as YouTube uploads.

## What it does

1. Pulls VOD metadata from the Twitch Helix API.
2. Fetches every clip created during that VOD's time window.
3. Filters to clips that actually reference the VOD (`video_id` match).
4. Scores each clip on views, clip density (how many other viewers clipped
   the same moment), duration sweet spot, and clipper diversity.
5. Prints a ranked terminal table with links back to the exact VOD
   timestamp, and optionally writes a JSON report.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your Twitch Client ID and secret
```

### Getting Twitch API credentials

1. Go to <https://dev.twitch.tv/console/apps> and sign in.
2. Click **Register Your Application**.
3. Fill in:
   - **Name:** anything unique to your account (e.g. `my-clipper`).
   - **OAuth Redirect URLs:** `http://localhost` (unused here but required).
   - **Category:** *Application Integration* (or *Other*).
4. Click **Create**.
5. On the app's page, copy the **Client ID** into `TWITCH_CLIENT_ID`.
6. Click **New Secret**, copy it into `TWITCH_CLIENT_SECRET`.

The script uses the client-credentials flow — no user login, no OAuth
redirect round-trip, just the ID and secret.

## Usage

```bash
python clipper.py https://www.twitch.tv/videos/123456789
```

Options:

- `--limit N` — rows to print (default 20).
- `--min-views N` — drop clips below this view count.
- `--json out.json` — also dump the full ranked report with score
  components for spreadsheet / editor workflows.

Example:

```bash
python clipper.py https://www.twitch.tv/videos/123456789 \
  --min-views 25 --limit 15 --json last_stream.json
```

## Browser version

`clipper.html` is a zero-build, single-file version that runs locally. Open
it directly (`file://…/clipper.html`) — no server, no dependencies.

Because it runs in the browser, the client-credentials flow the Python
CLI uses isn't available (the token endpoint doesn't send CORS headers,
and shipping a client secret in HTML is unsafe). Instead, paste a
pre-generated access token:

- With the [Twitch CLI](https://dev.twitch.tv/docs/cli/):
  ```bash
  twitch token
  ```
- Or one-shot via curl, using the same credentials as `.env`:
  ```bash
  curl -X POST https://id.twitch.tv/oauth2/token \
    -d "client_id=$TWITCH_CLIENT_ID&client_secret=$TWITCH_CLIENT_SECRET&grant_type=client_credentials"
  ```

Paste the Client ID and the resulting `access_token` into the page.
Both are cached in `localStorage` on that device only; a "clear" link
wipes them. The scoring and JSON output match the CLI exactly.

## How clips are scored

Each clip gets a composite 0–100 score:

| Signal                | Weight | Notes                                              |
| --------------------- | ------ | -------------------------------------------------- |
| View count (log)      | 0.50   | Normalized against the top clip in this VOD.       |
| Clip density (±30s)   | 0.25   | How many other clips sit around the same moment.   |
| Duration sweet spot   | 0.15   | Peaks at 45s, falls off outside 20–90s.            |
| Clipper diversity     | 0.10   | Unique clippers in the cluster — organic hype.     |

Timestamps in the printed table are clickable links back to the VOD at
the clipped moment, so you can verify the top picks quickly.

## Not included (yet)

- No video download / transcription.
- No chat-log analysis. (Easy to add later as an extra score component.)
- No YouTube upload automation.

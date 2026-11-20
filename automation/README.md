# Crime Scene Six automation

This folder connects the original `gtavi-claudecode` scripts to a free GitHub Actions runner and Composio publishing.

## What it does

1. Runs the original `scripts/streams.py` to find current GTA 6 live streams.
2. On scheduled runs, uses the original `scripts/live.py` to watch up to two of the leading streams for chat spikes. A two-minute capture is used only for a safe manual test; scheduled captures run for twelve minutes.
3. If no usable live spike is found, runs the original `scripts/trends.py` to find recent videos. If YouTube's original search route returns nothing, it tries yt-dlp's built-in YouTube search.
4. Uses the original `scripts/moments.py` to score moments from a source.
5. Uses the original `scripts/cut.py` to create a vertical 1080x1920 clip with captions and a hook.
6. Runs the original `scripts/check.py` before publication.
7. Sends the finished MP4 through Composio to YouTube, Facebook Page, Instagram Reels and TikTok.
8. Records which platforms accepted each source so retries do not intentionally repost to platforms that already accepted it.

The original discovery, recording, moment-scoring, cutting and checking scripts remain in place and are called directly.

## One-time setup

Add one GitHub Actions secret:

- Name: `COMPOSIO_API_KEY`
- Value: a **Project API key** from the same Composio Platform project that owns the four connected social accounts.

In GitHub open Settings → Secrets and variables → Actions → New repository secret.

The runner reads each connected account's ID and user ID from Composio. You do not need a separate `COMPOSIO_USER_ID` secret.

The Facebook Page is selected by the exact name `CrimeScene VI`. Instagram is checked against `crimescene_vi`, TikTok against `scurotwo8`, and YouTube is selected from the active YouTube connection. If the API key belongs to a different Composio project, the run will stop and say so rather than silently using a different account.

## Test first

Open Actions → Crime Scene Six Auto Clipper → Run workflow. Leave **Test clipping without publishing** enabled. The runner will search for live streams and recorded videos, prepare and validate one clip, but will not post anything.

After the test succeeds and the secret is added, scheduled runs publish automatically. The schedule checks every four hours. Each run handles one source and creates an artifact containing the clip and logs.

## Free-hosting limits

GitHub's free hosted runner is temporary. It is suitable for scheduled batches, not continuous 24/7 stream recording. The workflow checks streams at scheduled intervals; it does not keep a recorder open all day. A four-hour schedule can miss a short live moment between checks.

GitHub Actions on this public repository uses standard hosted runners without consuming the private-repository minutes allowance. The runner still has a maximum job duration and temporary disk space.

## Publishing and creator rights

The clip includes source credit. Only publish footage you have permission to reuse or that you can lawfully transform under the applicable platform and copyright rules. The workflow does not claim affiliation with Rockstar Games.

TikTok publishing requires the account to allow public posting. The workflow checks this before it uploads. A successful upload request is not always the same as a completed public post; TikTok processing is checked before the run records success.

# Crime Scene Six automation

This folder connects the original `gtavi-claudecode` scripts to a free GitHub Actions runner and Composio publishing.

## What it does

1. Runs the original `scripts/trends.py` to find recent GTA 6 videos.
2. Uses the original `scripts/moments.py` to score moments from the best candidate source.
3. Uses the original `scripts/cut.py` to create a vertical 1080x1920 clip with captions and a hook.
4. Runs the original `scripts/check.py` before publication.
5. Sends the finished MP4 through Composio to YouTube, Facebook Page, Instagram Reels and TikTok.
6. Records which platforms accepted each source so retries do not intentionally repost to platforms that already accepted it.

The original scripts remain in place and are called directly. This automation does not replace the plugin's existing tools.

## One-time setup

Add one GitHub Actions secret:

- Name: `COMPOSIO_API_KEY`
- Value: a **Project API key** from the same Composio Platform project that owns the four connected social accounts.

In GitHub open Settings → Secrets and variables → Actions → New repository secret.

The runner reads each connected account's ID and user ID from Composio. You do not need to create a separate `COMPOSIO_USER_ID` secret.

The Facebook Page is selected by the exact name `CrimeScene VI`. Instagram is checked against `crimescene_vi`, TikTok against `scurotwo8`, and YouTube is selected from the active YouTube connection. If the API key belongs to a different Composio project, the run will stop and say so rather than silently using a different account.

## Test first

Open Actions → Crime Scene Six Auto Clipper → Run workflow. Leave **Test clipping without publishing** enabled. The runner will discover, cut and validate one clip, but will not post anything.

After the test succeeds and the secret is added, the scheduled runs publish automatically. The schedule runs every four hours. Each run handles one source and creates an artifact containing the clip and logs.

## Important limits

GitHub's free hosted runner is temporary. It is suitable for scheduled batches, not continuous 24/7 stream recording. The original `live.py` recorder is not run as an always-on service by this workflow. The current automated path uses recent public videos discovered by the original trend finder.

The source creator's rights and each platform's rules still apply. The clip includes source credit and must not be used for leaks, piracy, fake news or other content prohibited by the original project rules.

TikTok publishing requires the account to allow public posting. The workflow checks this before it uploads. A successful upload request is not always the same as a completed public post; TikTok may finish processing asynchronously.

GitHub Actions on a public repository uses standard hosted runners without consuming the private-repository minutes allowance. The runner still has a maximum job duration and temporary disk space.

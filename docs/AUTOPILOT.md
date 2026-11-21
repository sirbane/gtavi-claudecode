# Autopilot

One command: `python scripts/autopilot.py`

find -> analyse (Kaggle model via ngrok, the only AI backend) -> edit -> caption -> post to Facebook and YouTube via Composio -> repeat.

- `python scripts/autopilot.py --status` shows posts today, Composio calls this month and whether the Kaggle server is online.
- `python scripts/autopilot.py --once` runs a single pass.
- Settings live in `.env` (copy `.env.example`). Caps: MAX_POSTS_PER_DAY_FACEBOOK / _YOUTUBE, MIN_GAP_MINUTES.
- Safety stays strict: a clip posts only if its window passed the visual safety check and a caption was written from real frames.
- Kaggle: open `kaggle/vision_server.ipynb` on Kaggle, add the secrets listed in its first cell, Run All, then put the printed URL in `KAGGLE_OLLAMA_URL`.

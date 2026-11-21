#!/usr/bin/env python3
"""Crime Scene Six autopilot. One command, runs forever:

    python scripts/autopilot.py

find sources -> analyse video (Kaggle model via ngrok, Groq/Ollama fallback) -> edit -> caption -> post to Facebook + YouTube
through Composio -> repeat. Status: python scripts/autopilot.py --status   One pass only: --once
Everything is configurable in .env (see .env.example). Safety stays strict: no completed visual review = no post.
"""
from __future__ import annotations
import os, sys, json, time, random, subprocess, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "automation")]
# Windows consoles and pipes default to cp1252 and crash on emoji in video titles.
# Force UTF-8 here, and make every child process inherit it.
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def load_env() -> None:
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
load_env()
os.chdir(ROOT)

import watch_and_caption as wc   # noqa: E402
import runner                    # noqa: E402

def cfg(k, d): return os.getenv(k) or d
PLATFORMS = [p.strip() for p in cfg("PLATFORMS", "facebook,youtube").split(",") if p.strip()]
CAP = {"facebook": int(cfg("MAX_POSTS_PER_DAY_FACEBOOK", "10")), "youtube": int(cfg("MAX_POSTS_PER_DAY_YOUTUBE", "5"))}
GAP_MIN = float(cfg("MIN_GAP_MINUTES", "45"))
SLEEP_MIN = float(cfg("LOOP_SLEEP_MINUTES", "15"))
MAX_READY = int(cfg("MAX_READY_CLIPS", "3"))
RUN_TIMEOUT = int(cfg("RUNNER_TIMEOUT_S", "5400"))
AUTOPOST = cfg("AUTOPOST", "1").lower() in ("1", "true", "yes")
STATE_PATH = ROOT / "automation" / "published.json"
LOG_PATH = ROOT / "autopilot.log"

def say(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"clips": {}, "posts": [], "composio_calls": {}}

def save_state(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    s["posts"] = s["posts"][-500:]
    STATE_PATH.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")

def count_calls(s: dict, n: int = 1) -> None:
    m = datetime.now(timezone.utc).strftime("%Y-%m")
    s.setdefault("composio_calls", {})[m] = s["composio_calls"].get(m, 0) + n

def posts_last_24h(s: dict, platform: str) -> list[datetime]:
    cut = datetime.now(timezone.utc) - timedelta(hours=24)
    out = [datetime.fromisoformat(p["t"]) for p in s["posts"] if p["p"] == platform]
    return [t for t in out if t > cut]

def allowed(s: dict, platform: str) -> tuple[bool, str]:
    recent = posts_last_24h(s, platform)
    if len(recent) >= CAP.get(platform, 5):
        return False, f"{platform} daily cap {CAP.get(platform, 5)} reached"
    if recent and (datetime.now(timezone.utc) - max(recent)).total_seconds() < GAP_MIN * 60:
        return False, f"{platform} min gap {GAP_MIN:.0f} min not reached"
    return True, ""

_ctx: dict = {}
def destinations(s: dict) -> dict:
    if _ctx and time.time() - _ctx["t"] < 1800:
        return _ctx
    accounts = runner.connected_accounts(); count_calls(s)
    ctx = {"t": time.time()}
    if "facebook" in PLATFORMS:
        try:
            ctx["facebook"], ctx["page_id"] = runner.get_facebook_page(accounts); count_calls(s)
        except Exception as e:
            say(f"Facebook unavailable: {str(e)[:200]}")
    if "youtube" in PLATFORMS:
        try:
            ctx["youtube"] = runner.account_for(accounts, "youtube")
        except Exception as e:
            say(f"YouTube unavailable (connect it in the Composio project behind your key): {str(e)[:160]}")
    _ctx.clear(); _ctx.update(ctx)
    return _ctx

def call(s: dict, acc: dict, slug: str, args: dict) -> dict:
    r = runner.api_post("/tools/execute/" + slug, {"connected_account_id": acc["id"], "user_id": acc.get("user_id"),
                                                   "version": "latest", "arguments": args}, timeout=600)
    count_calls(s)
    if r.get("successful") is False or r.get("error"):
        raise RuntimeError(str(r.get("error") or r)[:300])
    return r.get("data") or {}

def publish_item(s: dict, key: str, item: dict) -> None:
    rec = json.loads(Path(item["post"]).read_text(encoding="utf-8"))
    mp4 = Path(item["video"])
    dest = destinations(s)
    tags = [t.lstrip("#") for t in rec["caption"].split() if t.startswith("#")][:15]
    for platform in PLATFORMS:
        if platform in item["done"] or item["fails"].get(platform, 0) >= 3 or platform not in dest:
            continue
        ok, why = allowed(s, platform)
        if not ok:
            say(f"  waiting: {why}"); continue
        try:
            if platform == "facebook":
                ref = runner.stage_video(mp4, "facebook", "FACEBOOK_CREATE_VIDEO_POST"); count_calls(s)
                out = call(s, dest["facebook"], "FACEBOOK_CREATE_VIDEO_POST", {"page_id": dest["page_id"], "video": ref,
                           "title": (rec.get("facebook_title") or rec["caption"].split("\n")[0])[:100], "description": rec["caption"], "published": True})
            else:
                ref = runner.stage_video(mp4, "youtube", "YOUTUBE_MULTIPART_UPLOAD_VIDEO"); count_calls(s)
                out = call(s, dest["youtube"], "YOUTUBE_MULTIPART_UPLOAD_VIDEO", {"title": rec["youtube_title"], "description": rec["caption"],
                           "categoryId": "20", "privacyStatus": "public", "tags": tags, "videoFile": ref})
            item["done"][platform] = json.dumps(out)[:200]
            s["posts"].append({"t": datetime.now(timezone.utc).isoformat(), "p": platform, "clip": key})
            say(f"  POSTED to {platform}: {rec['caption'].splitlines()[0][:70]}")
        except Exception as e:
            item["fails"][platform] = item["fails"].get(platform, 0) + 1
            say(f"  {platform} failed ({item['fails'][platform]}/3): {str(e)[:220]}")
        save_state(s)
    if all(p in item["done"] or item["fails"].get(p, 0) >= 3 or p not in dest for p in PLATFORMS):
        item["status"] = "done"

def caption_new_clips(s: dict) -> None:
    guide = Path(wc.env("GUIDE_PATH")).read_text(encoding="utf-8")
    hp = Path(wc.env("HISTORY_PATH")); history = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else []
    for mp4 in sorted((ROOT / "jobs").glob("*/edits/clip_*.mp4"), key=lambda p: p.stat().st_mtime):
        job = mp4.parent.parent; key = f"{job.name}/{mp4.name}"
        old = s["clips"].get(key)
        if old and (old["status"] in ("ready", "done", "held") or old.get("tries", 0) >= 3):
            continue
        item = old or {"status": "new", "tries": 0, "done": {}, "fails": {}, "video": str(mp4)}
        item["tries"] += 1
        side = mp4.with_suffix(".json")
        meta = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        if (meta.get("safety") or {}).get("status") != "pass" or meta.get("needs_vision_review"):
            item.update(status="held", reason=f"safety gate: {(meta.get('safety') or {}).get('status')}")
            say(f"HELD (not posting) {key}: {item['reason']}"); s["clips"][key] = item; continue
        try:
            rec = wc.build(job, mp4, guide, history, ROOT / "artifacts" / "posts")
        except Exception as e:
            say(f"caption failed for {key}: {str(e)[:200]}"); s["clips"][key] = item; continue
        if not rec.get("caption") or any("BANNED" in n for n in rec.get("notes", [])):
            item["reason"] = "no caption (vision unavailable) or banned phrase"; s["clips"][key] = item
            say(f"HELD {key}: {item['reason']} (try {item['tries']}/3)"); continue
        item.update(status="ready", post=str(ROOT / "artifacts" / "posts" / job.name / "post.json"))
        s["clips"][key] = item; say(f"READY {key}: {rec['caption'].splitlines()[0][:80]}")
    hp.parent.mkdir(parents=True, exist_ok=True); hp.write_text(json.dumps(history[-50:], indent=2, ensure_ascii=False), encoding="utf-8")
    save_state(s)

def clip_stage() -> None:
    say("Looking for new content (find -> analyse -> edit)...")
    try:
        subprocess.run([sys.executable, str(ROOT / "automation" / "runner.py")], cwd=ROOT, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        say("clip stage timed out; continuing with whatever was finished")

def cycle(s: dict) -> bool:
    if AUTOPOST:
        for k, v in [(k, v) for k, v in s["clips"].items() if v["status"] == "ready"]:
            publish_item(s, k, v)
        save_state(s)
    ready = [k for k, v in s["clips"].items() if v["status"] == "ready"]
    if len(ready) < MAX_READY:
        clip_stage(); caption_new_clips(s)
        if AUTOPOST:
            for k, v in [(k, v) for k, v in s["clips"].items() if v["status"] == "ready"]:
                publish_item(s, k, v)
        save_state(s)
    return True

def status() -> None:
    s = load_state(); m = datetime.now(timezone.utc).strftime("%Y-%m")
    print(f"Platforms: {', '.join(PLATFORMS)} | autopost: {AUTOPOST} | safety: {os.getenv('SAFETY_MODE', 'strict')}")
    for p in PLATFORMS:
        print(f"  {p}: {len(posts_last_24h(s, p))}/{CAP.get(p, 5)} posts in the last 24h")
    calls = s.get("composio_calls", {}).get(m, 0)
    print(f"Composio tool calls this month (counted by this app): {calls} (free tiers are roughly 10,000 to 100,000 a month; check your dashboard)")
    by = {}
    for v in s["clips"].values(): by[v["status"]] = by.get(v["status"], 0) + 1
    print("Clips:", by or "none yet")
    url = os.getenv("KAGGLE_OLLAMA_URL")
    if url:
        import requests
        try:
            r = requests.get(url.rstrip("/") + "/api/tags", headers={"Authorization": "Bearer " + os.getenv("KAGGLE_API_KEY", ""), "ngrok-skip-browser-warning": "1"}, timeout=15)
            print("Kaggle model server:", "ONLINE" if r.ok else f"HTTP {r.status_code}")
        except Exception as e:
            print("Kaggle model server: OFFLINE (falls back to Groq):", str(e)[:80])
    else:
        print("Kaggle model server: not configured (using Groq, then Ollama Cloud)")

def main() -> None:
    if "--status" in sys.argv:
        return status()
    once = "--once" in sys.argv
    say(f"Autopilot started. Platforms: {', '.join(PLATFORMS)} | autopost: {AUTOPOST}. Ctrl+C to stop.")
    status()
    s = load_state()
    while True:
        try:
            cycle(s)
        except KeyboardInterrupt:
            raise
        except Exception:
            say("cycle error (continuing): " + traceback.format_exc()[-600:])
        if once:
            return
        wait = SLEEP_MIN * 60 * random.uniform(0.8, 1.2)
        say(f"Sleeping {wait / 60:.0f} min...")
        time.sleep(wait)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("Stopped by user.")

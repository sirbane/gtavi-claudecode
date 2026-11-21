#!/usr/bin/env python3
"""Watch each clip with a vision model, then write the post using docs/caption_guide.md.

Review-first: writes artifacts/posts/<job>/post.json + post.md. Nothing is published here.
Vision: Groq first, Ollama Cloud as fallback, transcript-only as the last resort (flagged in the output).
All settings come from env / .env.
"""
import os, re, sys, json, base64, subprocess, argparse, tempfile
from pathlib import Path
import requests

D = {
    "GROQ_VISION_MODEL": "qwen/qwen3.8-27b",
    "OLLAMA_VISION_MODEL": "gemma4:31b",
    "GROQ_MODEL": "openai/gpt-oss-120b",
    "FRAME_COUNT": "3",
    "FFMPEG_BIN": "ffmpeg",
    "HISTORY_PATH": "automation/caption_history.json",
    "GUIDE_PATH": "docs/caption_guide.md",
    "CAPTION_HASHTAGS": "GTA6,GTAVI,GrandTheftAuto6,GrandTheftAutoVI,RockstarGames,GTA6Trailer,GTA6Gameplay,GTA6News,ViceCity,GTA6Clips",
}
BANNED = ["get ready for", "dive into", "buckle up", "game changer", "don't miss", "dont miss", "imagine", "gta 6 gameplay clip",
          "check this out", "play early", "free download", "leak"]

def env(k):
    if os.getenv(k):
        return os.getenv(k)
    if os.path.exists(".env"):
        for line in open(".env"):
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip().strip('"')
    return D.get(k)

def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)

def duration(mp4):
    out = sh([env("FFMPEG_BIN"), "-i", str(mp4)]).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 30.0

def frames(mp4, n):
    dur = duration(mp4); out = []
    for i in range(n):
        t = dur * (0.04 + 0.92 * i / max(n - 1, 1))
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.jpg"
            sh([env("FFMPEG_BIN"), "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "6", str(f)])
            if f.exists():
                out.append((round(t, 1), base64.b64encode(f.read_bytes()).decode()))
    return dur, out

def creator(source):
    """Real handle from the Twitch clip itself; never guessed."""
    try:
        r = sh([sys.executable, "-m", "yt_dlp", "--skip-download", "--dump-single-json", source], timeout=90)
        j = json.loads(r.stdout)
        handle = (j.get("channel_url") or "").rstrip("/").split("/")[-1]
        if not handle or handle.isdigit():
            handle = j.get("uploader") or ""
        if handle and not handle.isdigit():
            return f"@{handle.lstrip('@')}", "Twitch"
    except Exception as e:
        print("creator lookup failed:", str(e)[:120])
    return None, None

VISION_PROMPT = """These are {n} frames from a short GTA 6 clip at seconds {ts}, plus the transcript (may be empty).
Describe only what you can actually see or read. Return ONLY JSON:
{{"summary":"2 sentences","peak_second":0,"peak_description":"what happens at the peak","on_screen_text":"text burned into the video or empty",
"genre":"reaction|scenery|glitch|detail|action","stranger_detail":"the one detail a non-gamer would find too real or too weird","ending":"what happens in the last seconds"}}
TRANSCRIPT: {tr}"""

def groq_chat(model, messages, json_mode=True):
    body = {"model": model, "messages": messages, "temperature": 0.7}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    import time
    for i in range(4):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {env('GROQ_API_KEY')}"}, json=body, timeout=120)
        if r.status_code == 429 and i < 3:
            time.sleep(10 * (i + 1)); continue
        break
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def parse(txt):
    return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))

def describe(fr, transcript):
    prompt = VISION_PROMPT.format(n=len(fr), ts=", ".join(str(t) for t, _ in fr), tr=transcript[:1500] or "none")
    if env("GROQ_API_KEY"):
        for k in (len(fr), 2):
            try:
                content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for _, b in fr[:k]]
                return parse(groq_chat(env("GROQ_VISION_MODEL"), [{"role": "user", "content": content}], json_mode=False)), "groq-vision"
            except Exception as e:
                print("groq vision failed:", str(e)[:140])
    if env("OLLAMA_API_KEY"):
        try:
            r = requests.post("https://ollama.com/api/chat", headers={"Authorization": f"Bearer {env('OLLAMA_API_KEY')}"}, timeout=180,
                              json={"model": env("OLLAMA_VISION_MODEL"), "stream": False,
                                    "messages": [{"role": "user", "content": prompt, "images": [b for _, b in fr]}]})
            r.raise_for_status()
            return parse(r.json()["message"]["content"]), "ollama-vision"
        except Exception as e:
            print("ollama vision failed:", str(e)[:140])
    return {"summary": "", "peak_second": None, "peak_description": "", "on_screen_text": "", "genre": "action",
            "stranger_detail": "", "ending": ""}, "transcript-only (no vision available)"

def clean(s):
    s = (s or "").replace("\u2014", ", ").replace("\u2013", "-")
    return re.sub(r"\s+,", ",", re.sub(r"  +", " ", s)).strip()

def banned_in(*texts):
    blob = " ".join(texts).lower()
    return [b for b in BANNED if b in blob]

def write_post(guide, desc, transcript, recent, retry_note=""):
    system = guide + """

OUTPUT: return ONLY JSON {"hook":"","line2":"","extra_tags":["max 2, no # sign"],"youtube_title":"","tiktok_title":"","facebook_title":"",
"hook_style":"one of the hook styles used","delivers":"why the clip delivers what the hook promises"}.
Do NOT write the credit line or the main hashtags; the system adds them. No em dashes. Hook 12 words or fewer.\nUse ONLY details present in WHAT THE CLIP SHOWS. Never invent vehicles, people, events or timestamps. This is a video game: never say it happened in real life."""
    user = f"WHAT THE CLIP SHOWS:\n{json.dumps(desc)}\n\nTRANSCRIPT:\n{transcript[:1200]}\n\nLAST 3 HOOKS (do not repeat their opening pattern):\n{json.dumps(recent)}\n{retry_note}"
    return parse(groq_chat(env("GROQ_MODEL"), [{"role": "system", "content": system}, {"role": "user", "content": user}]))

def build(job, mp4, guide, history, out_dir):
    cand = (job / "candidates.md").read_text(errors="replace") if (job / "candidates.md").exists() else ""
    src = (re.search(r"^source:\s*(\S+)", cand, re.M) or [None, ""])[1]
    transcript = " ".join(l[1:].strip() for l in cand.splitlines() if l.startswith(">"))
    dur, fr = frames(mp4, int(env("FRAME_COUNT")))
    desc, vision = describe(fr, transcript)
    if vision.startswith("transcript-only"):
        rec = dict(job=job.name, status="NEEDS VISION - no caption written", video=str(mp4), vision=vision, source=src, caption="", notes=["Vision models unavailable. Fix Ollama billing or Groq limits, then re-run."])
        d = out_dir / job.name; d.mkdir(parents=True, exist_ok=True)
        (d / "post.json").write_text(json.dumps(rec, indent=2))
        print("blocked (no vision)", job.name)
        return rec
    recent = [h["hook"] for h in history[-3:]]
    post = write_post(guide, desc, transcript, recent)
    for attempt in range(2):
        bad = banned_in(post.get("hook", ""), post.get("line2", ""), post.get("youtube_title", ""), post.get("tiktok_title", ""), post.get("facebook_title", ""))
        if not bad:
            break
        post = write_post(guide, desc, transcript, recent, f"\nYour last draft used banned phrases {bad}. Rewrite without them.")
    handle, platform = creator(src) if src else (None, None)
    credit = f"🎥 Original creator: {handle} ({platform})" if handle else f"🎥 Source: {src}"
    base = [t.strip().lstrip("#") for t in env("CAPTION_HASHTAGS").split(",") if t.strip()]
    extra = [t.lstrip("#") for t in (post.get("extra_tags") or [])][:2]
    tags = base + [t for t in extra if t.lower() not in {b.lower() for b in base}]
    hook, line2 = clean(post.get("hook")), clean(post.get("line2"))
    caption = f"{hook}\n{line2}\n{credit}\n\n" + " ".join("#" + t for t in tags)
    yt = clean(post.get("youtube_title"))
    if "#gta6" not in yt.lower():
        yt = (yt[:60] + " #GTA6").strip()
    yt = yt[:70]
    notes = []
    if not handle:
        notes.append("NEEDS HANDLE: creator lookup failed, credit uses the source link")
    if vision.startswith("transcript-only"):
        notes.append("NO VISION: caption written from transcript only, check it against the video")
    if banned_in(caption, yt):
        notes.append("BANNED PHRASE still present after retry")
    for m_, s_ in re.findall(r"\b(\d+):(\d\d)\b", caption + " " + yt):
        if int(m_) * 60 + int(s_) > dur:
            notes.append(f"timestamp {m_}:{s_} is past the end of the clip")
    if len(hook.split()) > 12:
        notes.append("hook is longer than 12 words")
    rec = dict(job=job.name, status="PENDING REVIEW", video=str(mp4), duration_s=round(dur, 1), vision=vision, creator=handle, source=src,
               caption=caption, youtube_title=yt, tiktok_title=clean(post.get("tiktok_title")), facebook_title=clean(post.get("facebook_title")),
               hook_style=post.get("hook_style"), delivers=post.get("delivers"), seen=desc, notes=notes)
    d = out_dir / job.name; d.mkdir(parents=True, exist_ok=True)
    (d / "post.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    (d / "post.md").write_text(f"# {job.name}  [PENDING REVIEW]\nvideo: {mp4}\nvision: {vision}\n\n## Caption (all pages)\n{caption}\n\n"
        f"## YouTube Short title\n{rec['youtube_title']}\n\n## TikTok title\n{rec['tiktok_title']}\n\n## Facebook Reel title\n{rec['facebook_title']}\n\n"
        f"## What the model saw\n{desc.get('summary','')}\npeak: {desc.get('peak_second')}s {desc.get('peak_description','')}\n\n"
        f"## Checks\n" + ("\n".join("- " + n for n in notes) or "- none") + "\n")
    history.append({"job": job.name, "hook": hook})
    print("captioned", job.name, "|", vision, "|", hook)
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-dir", default="jobs"); ap.add_argument("--out", default="artifacts/posts")
    a = ap.parse_args()
    if not env("GROQ_API_KEY"):
        print("GROQ_API_KEY not set - skipping captions"); return
    guide = Path(env("GUIDE_PATH")).read_text()
    hp = Path(env("HISTORY_PATH")); hp.parent.mkdir(parents=True, exist_ok=True)
    history = json.loads(hp.read_text()) if hp.exists() else []
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True); recs = []
    for job in sorted(Path(a.jobs_dir).glob("*")):
        mp4 = job / "edits" / "clip_1.mp4"
        if mp4.exists() and not (out / job.name / "post.json").exists():
            try:
                recs.append(build(job, mp4, guide, history, out))
            except Exception as e:
                print("caption failed", job.name, str(e)[:200])
    hp.write_text(json.dumps(history[-50:], indent=2, ensure_ascii=False))
    (out / "index.md").write_text("# Posts waiting for review\n\n" + "\n".join(f"- {r['job']}: {(r['caption'] or r['status']).splitlines()[0]}" for r in recs) + "\n")

if __name__ == "__main__":
    main()

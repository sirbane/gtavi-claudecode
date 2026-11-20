#!/usr/bin/env python3
"""Crime Scene Six unattended runner for the original gtavi-claudecode scripts.

Uses the repository's trends.py, moments.py, cut.py and check.py unchanged.
Composio handles all four publishing destinations. No social tokens are stored here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = ROOT / "automation"
ARTIFACTS = ROOT / "artifacts"
STATE_PATH = AUTOMATION / "state.json"
COMPOSIO_BASE = "https://backend.composio.dev/api/v3.1"
FACEBOOK_PAGE_NAME = os.getenv("FACEBOOK_PAGE_NAME", "CrimeScene VI")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "crimescene_vi")
YOUTUBE_CHANNEL_NAME = os.getenv("YOUTUBE_CHANNEL_NAME", "CrimeSceneVI")
TIKTOK_USERNAME = os.getenv("TIKTOK_USERNAME", "scurotwo8")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes"}
PLATFORMS = ("youtube", "facebook", "instagram", "tiktok")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CrimeSceneSix-AutoClipper/1.0"})

def say(message: str) -> None:
    print(message, flush=True)

def run(command: list[str], timeout: int | None = None) -> str:
    say("$ " + " ".join(map(str, command)))
    result = subprocess.run(
        command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout
    )
    output = result.stdout or ""
    print(output[-12000:], flush=True)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}")
    return output

def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"posted_sources": {}, "last_run": None}

def save_state(state: dict[str, Any]) -> None:
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

def composio_headers() -> dict[str, str]:
    key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "COMPOSIO_API_KEY is missing. Add it in GitHub: Settings > Secrets and variables > Actions. "
            "The user ID is read from the connected-account records; you do not need a separate user-ID secret."
        )
    return {"x-api-key": key, "Content-Type": "application/json"}

def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = SESSION.get(COMPOSIO_BASE + path, headers=composio_headers(), params=params, timeout=45)
    if not response.ok:
        raise RuntimeError(f"Composio request failed ({response.status_code}): {response.text[:1200]}")
    return response.json()

def api_post(path: str, payload: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    response = SESSION.post(
        COMPOSIO_BASE + path, headers=composio_headers(), json=payload, timeout=timeout
    )
    if not response.ok:
        raise RuntimeError(f"Composio request failed ({response.status_code}): {response.text[:1600]}")
    data = response.json()
    if data.get("successful") is False:
        raise RuntimeError(f"Composio action failed: {data.get('error') or data}")
    return data

def connected_accounts() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        result = api_get("/connected_accounts", params)
        items.extend(result.get("items") or [])
        cursor = result.get("next_cursor")
        if not cursor:
            break
    active = [
        x for x in items
        if str(x.get("status", "")).upper() == "ACTIVE"
        and not x.get("is_disabled", False)
    ]
    return active

def toolkit_slug(account: dict[str, Any]) -> str:
    return str((account.get("toolkit") or {}).get("slug", "")).lower()

def account_for(accounts: list[dict[str, Any]], slug: str) -> dict[str, Any]:
    matches = [a for a in accounts if toolkit_slug(a) == slug and a.get("id")]
    if not matches:
        raise RuntimeError(
            f"No active {slug} connection is visible to this Composio API key. "
            "The key must belong to the same Composio project that holds the connected social accounts."
        )
    return matches[0]

def execute_tool(account: dict[str, Any], slug: str, arguments: dict[str, Any], timeout: int = 240) -> dict[str, Any]:
    payload = {
        "connected_account_id": account["id"],
        "user_id": account.get("user_id"),
        "version": "latest",
        "arguments": arguments,
    }
    result = api_post("/tools/execute/" + slug, payload, timeout=timeout)
    return result.get("data") or {}

def stage_video(path: Path, toolkit: str, tool_slug: str) -> dict[str, str]:
    digestor = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digestor.update(chunk)
    request_data = api_post("/files/upload/request", {
        "toolkit_slug": toolkit,
        "tool_slug": tool_slug,
        "filename": path.name,
        "mimetype": "video/mp4",
        "md5": digestor.hexdigest(),
    })
    request_data = request_data.get("data") if isinstance(request_data.get("data"), dict) else request_data
    upload_url = request_data.get("new_presigned_url") or request_data.get("newPresignedUrl")
    s3key = request_data.get("key")
    if not upload_url or not s3key:
        raise RuntimeError(f"Composio did not return a usable upload location for {tool_slug}.")
    with path.open("rb") as handle:
        uploaded = SESSION.put(upload_url, data=handle, headers={"Content-Type": "video/mp4"}, timeout=600)
    if not uploaded.ok:
        raise RuntimeError(f"Could not transfer the finished clip to Composio ({uploaded.status_code}).")
    return {"name": path.name, "mimetype": "video/mp4", "s3key": s3key}

def nested_payload(result: dict[str, Any]) -> dict[str, Any]:
    # Keep provider responses such as Facebook's {data: [...], paging: ...} intact.
    value = result.get("data", result)
    return value if isinstance(value, dict) else result

def get_instagram_id(account: dict[str, Any]) -> str:
    result = execute_tool(account, "INSTAGRAM_GET_USER_INFO", {"ig_user_id": "me", "fields": "id,username"})
    data = nested_payload(result)
    username = str(data.get("username", "")).lower()
    ig_id = str(data.get("id", ""))
    if username and username != INSTAGRAM_USERNAME.lower():
        raise RuntimeError(f"Instagram connection points to @{username}, not @{INSTAGRAM_USERNAME}.")
    if not ig_id:
        raise RuntimeError("Instagram did not return its professional account ID.")
    return ig_id

def get_facebook_page(accounts: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    candidates = [a for a in accounts if toolkit_slug(a) == "facebook"]
    for account in candidates:
        try:
            result = execute_tool(
                account, "FACEBOOK_LIST_MANAGED_PAGES",
                {"limit": 100, "fields": "id,name,category,tasks,link"}
            )
            data = nested_payload(result)
            pages = data.get("data") or []
            for page in pages:
                if str(page.get("name", "")).strip().lower() == FACEBOOK_PAGE_NAME.lower():
                    if "CREATE_CONTENT" not in (page.get("tasks") or []):
                        raise RuntimeError(f"Facebook Page {FACEBOOK_PAGE_NAME} is connected but cannot create content.")
                    return account, str(page["id"])
        except Exception as exc:
            say(f"Facebook connection check: {exc}")
    raise RuntimeError(
        f"Could not find the Facebook Page '{FACEBOOK_PAGE_NAME}' in the connected Facebook accounts."
    )

def verify_destinations(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    youtube = account_for(accounts, "youtube")
    tiktok = account_for(accounts, "tiktok")
    instagram = account_for(accounts, "instagram")
    facebook, page_id = get_facebook_page(accounts)

    creator = nested_payload(execute_tool(tiktok, "TIKTOK_QUERY_CREATOR_INFO", {}))
    options = creator.get("privacy_level_options") or []
    if "PUBLIC_TO_EVERYONE" not in options:
        raise RuntimeError(
            "TikTok does not currently allow public posting for this account. "
            "No TikTok video will be posted until public visibility is enabled."
        )
    nickname = str(creator.get("creator_username") or "").lower()
    if nickname and nickname != TIKTOK_USERNAME.lower():
        raise RuntimeError(f"TikTok connection points to @{nickname}, not @{TIKTOK_USERNAME}.")

    ig_id = get_instagram_id(instagram)
    say(f"Publishing destinations verified: YouTube / {FACEBOOK_PAGE_NAME} / @{INSTAGRAM_USERNAME} / @{TIKTOK_USERNAME}")
    return {
        "youtube": youtube, "facebook": facebook, "facebook_page_id": page_id,
        "instagram": instagram, "instagram_user_id": ig_id, "tiktok": tiktok,
    }

def publish_one(clip: Path, source: dict[str, Any], destinations: dict[str, Any], already: list[str]) -> list[str]:
    title = str(source.get("title") or "GTA 6 moment").strip()
    channel = str(source.get("channel") or "Original creator").strip()
    source_url = str(source.get("url") or "").strip()
    caption = (
        f"{title[:110]}\n\nOriginal stream/video: {channel}. "
        f"Source: {source_url}\nEdited vertical clip with captions. "
        "#GTA6 #GTAVI #CrimeSceneSix #Shorts"
    )
    description = (
        f"{title}\n\nOriginal creator: {channel}\nOriginal source: {source_url}\n"
        "Edited vertical highlight with captions. Unofficial fan content; not affiliated with Rockstar Games."
    )
    succeeded: list[str] = []
    if "youtube" not in already:
        try:
            file_ref = stage_video(clip, "youtube", "YOUTUBE_UPLOAD_VIDEO")
            result = execute_tool(destinations["youtube"], "YOUTUBE_UPLOAD_VIDEO", {
                "title": (title[:85] + " #Shorts")[:100],
                "description": description,
                "tags": ["GTA 6", "GTA VI", "Crime Scene Six", "Grand Theft Auto VI", "Shorts"],
                "categoryId": "20",
                "privacyStatus": "public",
                "videoFilePath": file_ref,
            }, timeout=600)
            say("YouTube upload accepted: " + json.dumps(result)[:600])
            succeeded.append("youtube")
        except Exception as exc:
            say("YouTube publishing failed: " + str(exc))
    if "facebook" not in already:
        try:
            file_ref = stage_video(clip, "facebook", "FACEBOOK_CREATE_VIDEO_POST")
            result = execute_tool(destinations["facebook"], "FACEBOOK_CREATE_VIDEO_POST", {
                "page_id": destinations["facebook_page_id"],
                "video": file_ref,
                "title": title[:100],
                "description": description,
                "published": True,
            }, timeout=600)
            say("Facebook post accepted: " + json.dumps(result)[:600])
            succeeded.append("facebook")
        except Exception as exc:
            say("Facebook publishing failed: " + str(exc))
    if "instagram" not in already:
        try:
            file_ref = stage_video(clip, "instagram", "INSTAGRAM_POST_IG_USER_MEDIA")
            created = execute_tool(destinations["instagram"], "INSTAGRAM_POST_IG_USER_MEDIA", {
                "ig_user_id": destinations["instagram_user_id"],
                "video_file": file_ref,
                "caption": caption[:2200],
                "media_type": "REELS",
                "share_to_feed": True,
                "thumb_offset": 1000,
            }, timeout=300)
            container = nested_payload(created).get("id")
            if not container:
                raise RuntimeError(f"Instagram did not return a Reel container ID: {created}")
            published = execute_tool(destinations["instagram"], "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {
                "ig_user_id": destinations["instagram_user_id"],
                "creation_id": str(container),
                "max_wait_seconds": 240,
                "poll_interval_seconds": 4,
            }, timeout=360)
            say("Instagram Reel published: " + json.dumps(published)[:600])
            succeeded.append("instagram")
        except Exception as exc:
            say("Instagram publishing failed: " + str(exc))
    if "tiktok" not in already:
        try:
            file_ref = stage_video(clip, "tiktok", "TIKTOK_UPLOAD_VIDEO")
            result = execute_tool(destinations["tiktok"], "TIKTOK_UPLOAD_VIDEO", {
                "file_to_upload": file_ref,
                "caption": caption[:2200],
                "publish": True,
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "is_aigc": False,
            }, timeout=600)
            status_data = nested_payload(result)
            publish_id = status_data.get("publish_id")
            if not publish_id:
                raise RuntimeError(f"TikTok did not return a publish ID: {result}")
            delay = 5
            final_status = ""
            for attempt in range(5):
                time.sleep(delay)
                status_result = execute_tool(
                    destinations["tiktok"], "TIKTOK_FETCH_PUBLISH_STATUS",
                    {"publish_id": str(publish_id)}, timeout=90
                )
                status_payload = nested_payload(status_result)
                final_status = str(status_payload.get("status") or "")
                say(f"TikTok processing status: {final_status or 'not returned'}")
                if final_status == "PUBLISH_COMPLETE":
                    break
                if final_status in {"FAILED", "PUBLISH_FAILED"}:
                    raise RuntimeError(f"TikTok rejected the video: {status_payload.get('fail_reason', final_status)}")
                delay = min(delay * 2, 30)
            if final_status != "PUBLISH_COMPLETE":
                raise RuntimeError(f"TikTok is not confirmed published yet (status: {final_status or 'unknown'}). It will be retried on a later run only if safe.")
            succeeded.append("tiktok")
        except Exception as exc:
            say("TikTok publishing failed: " + str(exc))
    return succeeded

def parse_trends(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    pattern = re.compile(r"^\|\s*\d+\s*\|.*?\|\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*\|", re.I)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if match:
            rows.append({"title": match.group(1).strip(), "url": match.group(2).strip(), "channel": ""})
    return rows



def ytsearch_fallback() -> list[dict[str, str]]:
    """Fallback to yt-dlp's built-in YouTube search if the original trend URL returns no entries."""
    queries = ["ytsearchdate30:GTA 6", "ytsearch30:GTA 6", "ytsearchdate30:Grand Theft Auto VI", "ytsearch30:Grand Theft Auto VI"]
    for query in queries:
        command = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress", "-J", "--flat-playlist", "--playlist-end", "30", query]
        say("Fallback search: " + query)
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if result.returncode != 0 or not result.stdout.strip():
            say("Fallback search returned no result: " + (result.stderr or "no output")[-500:])
            continue
        try:
            payload = json.loads(result.stdout)
        except Exception as exc:
            say("Could not read fallback search results: " + str(exc))
            continue
        rows = []
        for entry in payload.get("entries") or []:
            if not entry:
                continue
            video_url = entry.get("webpage_url") or entry.get("url") or ""
            video_id = entry.get("id") or ""
            if not video_url.startswith("http") and video_id:
                video_url = "https://www.youtube.com/watch?v=" + video_id
            if video_url.startswith("http"):
                rows.append({"title": entry.get("title") or "GTA 6 video", "url": video_url, "channel": entry.get("channel") or entry.get("uploader") or ""})
        if rows:
            say(f"Built-in YouTube search found {len(rows)} candidate videos.")
            return rows
    return []


def parse_streams(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    pattern = re.compile(r"^\|\s*\d+\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*(https?://[^|]+)\s*\|", re.I)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if match:
            rows.append({"channel": match.group(1).strip(), "title": match.group(2).strip(), "viewers": match.group(3).strip(), "url": match.group(5).strip()})
    return rows


def job_folder_for(source_path: str) -> Path:
    import hashlib
    from urllib.parse import urlparse
    if source_path.startswith(("http://", "https://")):
        stem_source = source_path.split("v=")[-1].split("/")[-1].lower()
    else:
        stem_source = Path(source_path).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem_source)[:24].strip("-") or "job"
    key = hashlib.sha1(source_path.encode()).hexdigest()[:8]
    return ROOT / "jobs" / f"{stem}-{key}"


def build_caption(source: dict[str, Any], candidate: dict[str, Any]) -> str:
    channel = source.get("channel") or "the original creator"
    title = source.get("title") or "GTA 6"
    text = str(candidate.get("text") or "").strip()
    # A short, accurate hook derived from the source title; no invented gameplay claims.
    hook = re.sub(r"\s+", " ", title).strip()
    return (hook[:52] if hook else "GTA 6 moment")

def make_clip(source: dict[str, str]) -> tuple[Path, dict[str, Any]] | None:
    source_path = source.get("local_file") or source["url"]
    folder = job_folder_for(source_path)
    run([sys.executable, "scripts/moments.py", source_path, "--top", "5", "--min", "15", "--max", "40", "--max-minutes", "25"], timeout=5400)
    candidates_path = folder / "candidates.json"
    if not candidates_path.exists():
        raise RuntimeError(f"The original moment finder did not create {candidates_path}.")
    data = json.loads(candidates_path.read_text(encoding="utf-8"))
    picks = data.get("candidates") or []
    if not picks:
        return None
    best = max(picks, key=lambda item: float(item.get("score") or 0))
    if float(best.get("score") or 0) <= 0:
        return None
    hook = build_caption(source, best)
    run([sys.executable, "scripts/cut.py", str(folder), "--pick", str(best["n"]), "--layout", "blur", "--hook", hook, "--captions", "word"], timeout=3600)
    clip = folder / "edits" / f"clip_{best['n']}.mp4"
    if not clip.exists():
        raise RuntimeError("The original clip cutter did not produce an MP4.")
    sidecar = clip.with_suffix(".json")
    meta = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    info = data.get("info") or {}
    if not source.get("local_file"):
        source.update({"title": info.get("title") or source.get("title") or "GTA 6 moment", "channel": info.get("channel") or info.get("uploader") or source.get("channel") or "Original creator"})
    else:
        source["title"] = source.get("title") or "GTA 6 live-stream moment"
        source["channel"] = source.get("channel") or "Original creator"
    meta.update({"source_url": source["url"], "source_title": source["title"], "source_channel": source["channel"]})
    sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    caption = f"{source['title']}\n\nOriginal creator: {source['channel']}\nSource: {source['url']}\n#GTA6 #GTAVI #CrimeSceneSix #Shorts"
    run([sys.executable, "scripts/check.py", str(clip), "--caption", caption], timeout=120)
    return clip, source


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    state = load_state()
    state.setdefault("posted_sources", {})
    say("Crime Scene Six automation starting.")
    say("Mode: " + ("TEST ONLY, nothing will be published." if DRY_RUN else "AUTOMATIC PUBLICATION ENABLED."))
    destinations = None
    if not DRY_RUN:
        accounts = connected_accounts()
        say("Active Composio connections visible to this API key: " + ", ".join(sorted({toolkit_slug(x) for x in accounts})))
        destinations = verify_destinations(accounts)

    # Scheduled runs first watch a live stream for chat spikes, using the original live.py recorder.
    live_sources: list[dict[str, str]] = []
    try:
        run([sys.executable, "scripts/streams.py", "--query", "GTA 6", "--limit", "5", "--out", "streams.md"], timeout=180)
        live_sources = parse_streams(ROOT / "streams.md")
        say(f"Live-stream search returned {len(live_sources)} possible streams.")
    except Exception as exc:
        say("Live-stream discovery was unavailable; falling back to recorded videos: " + str(exc))

    for live_source in live_sources[:2]:
        url = live_source["url"]
        if set(state["posted_sources"].get(url, [])) >= set(PLATFORMS):
            continue
        try:
            capture_minutes = "2" if DRY_RUN else "12"
            run([sys.executable, "scripts/live.py", url, "--minutes", capture_minutes], timeout=1000)
            live_folder = job_folder_for(url) / "live"
            spikes = sorted(live_folder.glob("spike_*.mp4"))
            for spike in spikes:
                source = {"url": url, "local_file": str(spike.resolve()), "title": live_source.get("title") or "GTA 6 live-stream moment", "channel": live_source.get("channel") or "Original creator"}
                made = make_clip(source)
                if not made:
                    continue
                clip, source = made
                target = ARTIFACTS / clip.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(clip.read_bytes())
                done = state["posted_sources"].get(url, [])
                if DRY_RUN:
                    say(f"TEST COMPLETE: prepared live clip {clip}. No posts were made.")
                    return 0
                succeeded = publish_one(clip, source, destinations, done)
                state["posted_sources"][url] = sorted(set(done + succeeded))
                save_state(state)
                return 0 if succeeded else 1
        except Exception as exc:
            say("Live stream did not produce a publishable clip; trying recorded videos: " + str(exc))

    # VOD fallback: first 24 hours, then 7 days, then 30 days if the GTA 6 upload window is quiet.
    sources: list[dict[str, str]] = []
    for hours in (24, 168, 720):
        run([sys.executable, "scripts/trends.py", "--query", "GTA 6", "--hours", str(hours), "--limit", "30", "--out", "trends.md"], timeout=900)
        sources = parse_trends(ROOT / "trends.md")
        if sources:
            say(f"Found {len(sources)} candidate videos in the last {hours} hours.")
            break
        say(f"No videos found in the last {hours} hours; widening the search.")
    if not sources:
        sources = ytsearch_fallback()
    if not sources:
        raise RuntimeError("The original trend search and the built-in YouTube search found no usable GTA 6 videos or live clips. No content was published.")

    for source in sources[:5]:
        url = source["url"]
        done = state["posted_sources"].get(url, [])
        if set(done) >= set(PLATFORMS):
            say("Already posted to all four platforms; skipping " + url)
            continue
        try:
            made = make_clip(source)
        except Exception as exc:
            say("Could not process this source; trying the next trend: " + str(exc))
            continue
        if not made:
            say("No usable moment found in " + url)
            continue
        clip, source = made
        target = ARTIFACTS / clip.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(clip.read_bytes())
        if DRY_RUN:
            say(f"TEST COMPLETE: prepared {clip} ({clip.stat().st_size:,} bytes). No posts were made.")
            save_state(state)
            return 0
        succeeded = publish_one(clip, source, destinations, done)
        state["posted_sources"][url] = sorted(set(done + succeeded))
        save_state(state)
        say("Published successfully to: " + (", ".join(succeeded) if succeeded else "none"))
        if set(state["posted_sources"][url]) >= set(PLATFORMS):
            say("All four platforms have accepted this source.")
        else:
            say("Some platforms remain pending. The next run will retry only the missing destinations.")
        return 0 if succeeded else 1
    raise RuntimeError("No new eligible source produced a clip. No content was published.")

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        say("AUTOMATION STOPPED: " + str(error))
        raise

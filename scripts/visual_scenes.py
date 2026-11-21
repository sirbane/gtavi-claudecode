#!/usr/bin/env python3
"""Visual scene discovery for Crime Scene Six.

Watches the whole source (uniform + adaptive frames), extracts timestamped events, scores them with explainable
components, applies a window-level safety verdict, and merges the result into candidates.json so the existing
cut/check/caption pipeline consumes it. Reuses the Groq/Ollama vision adapters from watch_and_caption.py.

Outputs per job: visual_scenes.json (schema_version 1.0), visual_scenes.md, candidates.json (merged),
candidates_audio.json (the untouched audio/transcript candidates). Nothing here publishes anything.
"""
from __future__ import annotations
import os, re, sys, json, math, time, hashlib, base64, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import watch_and_caption as wc

SCHEMA_VERSION = "1.0"
POLICY_VERSION = "general-audience-1"
TAXONOMY = ("unexpected_npc_behaviour", "npc_npc_interaction", "npc_player_interaction", "physics_animation", "vehicle_chaos",
            "animals_wildlife", "environmental_realism", "emergent_chaos", "streamer_reaction", "hidden_detail", "social_non_explicit")
SAFETY_FLAGS = ("nudity", "sexual_activity", "sexualized_framing", "suggestive_interaction", "sexual_text", "minor_ambiguous",
                "graphic_injury", "other_sensitive")
REJECT_ADULT_FLAGS = {"nudity", "sexual_activity", "sexual_text", "sexualized_framing"}
HOLD_FLAGS = {"suggestive_interaction", "minor_ambiguous", "other_sensitive"}
COMPONENTS = ("visual_surprise", "event_clarity", "novelty", "story_arc", "payoff_strength", "context_independence",
              "phone_readability", "reaction_strength", "social_commentability")
DEFAULT_WEIGHTS = {"visual_surprise": .18, "event_clarity": .12, "novelty": .12, "story_arc": .12, "payoff_strength": .12,
                   "context_independence": .08, "phone_readability": .08, "reaction_strength": .06, "social_commentability": .05,
                   "support": .04, "source_velocity": .03}
DEFAULTS = {
    "VISUAL_ANALYSIS_ENABLED": "true", "VISUAL_FRAME_INTERVAL_S": "2.0", "VISUAL_MAX_FRAMES": "30", "VISUAL_FRAME_MAX_WIDTH": "640",
    "VISUAL_JPEG_QUALITY": "65", "VISUAL_FRAMES_PER_REQUEST": "3", "VISUAL_MIN_CONFIDENCE": "0.6", "VISUAL_MIN_EVENT_SCORE": "0.45",
    "VISUAL_MAX_EVENTS_PER_SOURCE": "5", "VISUAL_MIN_CLIP_S": "8", "VISUAL_MAX_CLIP_S": "40", "VISUAL_PREROLL_S": "1.5",
    "VISUAL_POSTROLL_S": "2.0", "VISUAL_MERGE_GAP_S": "3.0", "VISUAL_CACHE_ENABLED": "true", "VISUAL_SCORING_WEIGHTS": "",
}

def cfg(k: str) -> str:
    v = wc.env(k)
    return v if v not in (None, "") else DEFAULTS.get(k, "")

def cfgf(k: str) -> float:
    return float(cfg(k))

def weights() -> dict[str, float]:
    w = dict(DEFAULT_WEIGHTS)
    raw = cfg("VISUAL_SCORING_WEIGHTS")
    if raw:
        try:
            w.update({k: float(v) for k, v in json.loads(raw).items() if k in w})
        except Exception:
            pass
    return w

def clamp01(x: Any) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(x) else max(0.0, min(1.0, x))

# ---------- frame sampling ----------
def plan_timestamps(duration: float, interval: float, max_frames: int, focus=(), min_gap: float = 0.8, edge: float = 0.3) -> list[float]:
    """Uniform coverage of the whole source plus extra samples inside focus windows (audio/chat/heatmap peaks)."""
    if duration <= 0 or max_frames <= 0:
        return []
    lo, hi = min(edge, duration / 2), max(duration - edge, duration / 2)
    uniform_budget = max_frames if not focus else max(3, max_frames // 3)
    n = max(1, min(uniform_budget, int(duration / max(interval, 0.1)) + 1))
    times = [lo + (hi - lo) * i / max(n - 1, 1) for i in range(n)] if n > 1 else [duration / 2]
    extra = max_frames - len(times)
    wins = [(max(lo, float(s)), min(hi, float(e))) for s, e in focus if float(e) > float(s)]
    if wins and extra > 0:
        per = max(1, extra // len(wins))
        for s, e in wins:
            k = min(per, int((e - s) / max(interval, 0.1)) + 1)
            times += [s + (e - s) * i / max(k - 1, 1) for i in range(k)] if k > 1 else [(s + e) / 2]
    times = sorted(min(max(t, lo), hi) for t in times)
    kept: list[float] = []
    for t in times:
        if not kept or t - kept[-1] >= min_gap:
            kept.append(t)
    while len(kept) > max_frames:           # thin evenly, never truncate the tail
        step = len(kept) / max_frames
        kept = [kept[int(i * step)] for i in range(max_frames)]
    return [round(t, 2) for t in kept]

def extract_frames(video: Path, times: list[float]) -> tuple[list[tuple[float, str]], list[float]]:
    width, quality = int(cfgf("VISUAL_FRAME_MAX_WIDTH")), int(cfgf("VISUAL_JPEG_QUALITY"))
    q = max(2, min(31, round(31 - quality * 0.29)))
    ok, failed = [], []
    for t in times:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.jpg"
            wc.sh([wc.env("FFMPEG_BIN"), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                   "-vf", f"scale='min({width},iw)':-2", "-q:v", str(q), str(f)])
            if f.exists() and f.stat().st_size > 0:
                ok.append((t, base64.b64encode(f.read_bytes()).decode()))
            else:
                failed.append(t)
    return ok, failed

# ---------- vision ----------
FRAME_PROMPT = """You are analysing {n} timestamped frames from a GTA VI gameplay video, at seconds {ts}.
Describe only visible evidence. Identify characters, NPCs, vehicles, objects, environmental details, interactions, movement and any
unexpected event. Separate what is directly visible from what is uncertain. Never infer thoughts, motives, identity, age or dialogue.
Never sexualize characters. Do not claim a bug, secret or feature unless the frames support it. A single still cannot prove a reaction.
Return ONLY JSON:
{{"observations":[{{"timestamp_s":0,"visible_entities":[],"actions":[],"interaction":"","unexpectedness":"yes|likely|uncertain|no","evidence":"","confidence":0.0,"uncertainty":[]}}],
"possible_events":[{{"start_s":0,"end_s":0,"category":"one of: {cats}","description":"","trigger":"","escalation":"","payoff":"","confidence":0.0}}],
"safety_flags":[{{"timestamp_s":0,"flag":"one of: {flags}"}}]}}
Flag nudity, sexual activity, sexualized camera framing, suggestive character interactions, sexual text, characters of unclear age in
suggestive context, graphic injury. Use an empty safety_flags list only if you saw none."""

def observe(frames: list[tuple[float, str]]) -> dict[str, Any]:
    per = max(1, int(cfgf("VISUAL_FRAMES_PER_REQUEST")))
    out = {"observations": [], "possible_events": [], "flags": [], "ok_times": [], "providers": set(), "errors": 0, "chunks": 0}
    for i in range(0, len(frames), per):
        grp = frames[i:i + per]; out["chunks"] += 1
        prompt = FRAME_PROMPT.format(n=len(grp), ts=", ".join(str(t) for t, _ in grp), cats=", ".join(TAXONOMY), flags=", ".join(SAFETY_FLAGS))
        j, prov = wc.vision_json(prompt, [b for _, b in grp])
        if not isinstance(j, dict):
            out["errors"] += 1; continue
        out["providers"].add(prov); out["ok_times"] += [t for t, _ in grp]
        for o in j.get("observations") or []:
            if isinstance(o, dict):
                o["timestamp_s"] = float(o.get("timestamp_s") or grp[0][0]); o["confidence"] = clamp01(o.get("confidence")); out["observations"].append(o)
        for e in j.get("possible_events") or []:
            if isinstance(e, dict):
                out["possible_events"].append(e)
        for fl in j.get("safety_flags") or []:
            name = fl.get("flag") if isinstance(fl, dict) else fl
            if name:
                out["flags"].append((float(fl.get("timestamp_s", grp[0][0])) if isinstance(fl, dict) else grp[0][0], str(name)))
    return out

MERGE_SYSTEM = """You receive timestamped observations and possible events from one GTA VI video. Merge them into distinct visual events.
Only use what the observations state. Split unrelated moments; merge one continuous story (setup, trigger, escalation, payoff).
A generic walk, drive, aim or idle scene is NOT an event; return an empty list when nothing distinct happens.
Score 0.0-1.0: 0.2 ordinary, 0.5 distinct but predictable, 0.8 unusually surprising and clear. Confidence is separate from quality.
Return ONLY JSON {"events":[{"start_s":0,"end_s":0,"primary_category":"<taxonomy>","secondary_categories":[],"summary":"","trigger":"",
"escalation":"","payoff":"","viewer_question":"","hook_fact":"what is visibly happening, plain words","evidence_timestamps":[],
"visual_surprise":0,"event_clarity":0,"novelty":0,"story_arc":0,"payoff_strength":0,"context_independence":0,"phone_readability":0,
"reaction_strength":0,"social_commentability":0,"confidence":0,"uncertainties":[]}]}
Taxonomy: """ + ", ".join(TAXONOMY)

def text_json(system: str, user: str) -> dict | None:
    import requests
    if wc.env("GROQ_API_KEY"):
        try:
            return wc.parse(wc.groq_chat(wc.env("GROQ_MODEL"), [{"role": "system", "content": system}, {"role": "user", "content": user}]))
        except Exception as e:
            print("visual merge (groq) failed:", str(e)[:120])
    if wc.env("OLLAMA_API_KEY"):
        try:
            r = requests.post("https://ollama.com/api/chat", headers={"Authorization": f"Bearer {wc.env('OLLAMA_API_KEY')}"}, timeout=120,
                              json={"model": wc.env("OLLAMA_TEXT_MODEL") or "gpt-oss:120b", "stream": False,
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return wc.parse(r.json()["message"]["content"])
        except Exception as e:
            print("visual merge (ollama) failed:", str(e)[:120])
    return None

# ---------- events ----------
_UNEXPECTED = {"yes": .8, "likely": .7, "uncertain": .4, "no": .1}

def events_from_observations(obs: list[dict], possible: list[dict]) -> list[dict]:
    """Deterministic fallback/test path: build events straight from the model's own possible_events."""
    events = []
    for pe in possible:
        try:
            s, e = float(pe["start_s"]), float(pe["end_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        inside = [o for o in obs if s - 0.5 <= o["timestamp_s"] <= e + 0.5]
        conf = sum(o.get("confidence", 0) for o in inside) / len(inside) if inside else clamp01(pe.get("confidence"))
        surprise = max([_UNEXPECTED.get(str(o.get("unexpectedness")).lower(), .4) for o in inside] or [.3])
        has_payoff = bool(pe.get("payoff")) and "not yet" not in str(pe.get("payoff")).lower() and "none" != str(pe.get("payoff")).lower()
        events.append({"start_s": s, "end_s": e, "primary_category": pe.get("category"), "secondary_categories": [],
                       "summary": pe.get("description", ""), "trigger": pe.get("trigger", ""), "escalation": pe.get("escalation", ""),
                       "payoff": pe.get("payoff", ""), "viewer_question": "", "hook_fact": pe.get("description", ""),
                       "evidence_timestamps": sorted({round(o["timestamp_s"], 1) for o in inside}),
                       "visual_surprise": surprise, "event_clarity": conf, "novelty": .5,
                       "story_arc": min(.95, .25 + .15 * len(inside) + (.2 if has_payoff else 0)),
                       "payoff_strength": .7 if has_payoff else .25, "context_independence": conf * .9, "phone_readability": .6,
                       "reaction_strength": .4, "social_commentability": .5, "confidence": clamp01(conf * .85),
                       "uncertainties": ["heuristic scoring: no merge model available"], "heuristic": True})
    return events

def normalize_event(ev: dict, duration: float) -> dict | None:
    try:
        s, e = float(ev["start_s"]), float(ev["end_s"])
    except (KeyError, TypeError, ValueError):
        return None
    s, e = max(0.0, s), min(duration, e)
    cat = ev.get("primary_category") or ev.get("category")
    if e <= s or cat not in TAXONOMY:
        return None
    out = dict(ev); out.update(start_s=round(s, 2), end_s=round(e, 2), primary_category=cat)
    out["secondary_categories"] = [c for c in (ev.get("secondary_categories") or []) if c in TAXONOMY and c != cat]
    for k in COMPONENTS + ("confidence",):
        out[k] = clamp01(ev.get(k))
    out["evidence_timestamps"] = sorted({round(float(t), 1) for t in (ev.get("evidence_timestamps") or []) if _isnum(t) and 0 <= float(t) <= duration})
    out["uncertainties"] = [str(u) for u in (ev.get("uncertainties") or [])]
    return out

def _isnum(x) -> bool:
    try:
        float(x); return True
    except (TypeError, ValueError):
        return False

def merge_adjacent(events: list[dict], gap: float) -> list[dict]:
    """Merge events of the same category that touch or sit within `gap` seconds into one continuous story."""
    out: list[dict] = []
    for ev in sorted(events, key=lambda x: x["start_s"]):
        prev = out[-1] if out else None
        if prev and prev["primary_category"] == ev["primary_category"] and ev["start_s"] <= prev["end_s"] + gap:
            w1, w2 = prev["end_s"] - prev["start_s"], ev["end_s"] - ev["start_s"]
            for k in COMPONENTS + ("confidence",):
                prev[k] = round((prev[k] * w1 + ev[k] * w2) / max(w1 + w2, 1e-6), 3)
            prev["end_s"] = max(prev["end_s"], ev["end_s"])
            prev["evidence_timestamps"] = sorted(set(prev["evidence_timestamps"]) | set(ev["evidence_timestamps"]))
            prev["secondary_categories"] = sorted(set(prev["secondary_categories"]) | set(ev["secondary_categories"]))
            prev["payoff"] = ev.get("payoff") or prev.get("payoff")
            prev["uncertainties"] = sorted(set(prev["uncertainties"]) | set(ev["uncertainties"]))
        else:
            out.append(dict(ev))
    return out

# ---------- safety on the exact window ----------
def window_safety(start: float, end: float, flags: list[tuple[float, str]], ok_times: list[float], min_frames: int = 2) -> dict:
    inside = [t for t in ok_times if start - 0.5 <= t <= end + 0.5]
    hit = sorted({f for t, f in flags if start - 1.0 <= t <= end + 1.0})
    base = {"flags": hit, "policy_version": POLICY_VERSION}
    if len(inside) < min_frames:
        return {**base, "status": "analysis_failed", "confidence": 0.0, "review_required": True,
                "reason": f"only {len(inside)} analysed frame(s) inside the window; cannot verify it"}
    if set(hit) & REJECT_ADULT_FLAGS:
        return {**base, "status": "reject_adult", "confidence": 0.9, "review_required": False, "reason": "adult or sexualized content flagged in this window"}
    if "graphic_injury" in hit:
        return {**base, "status": "reject_other", "confidence": 0.85, "review_required": False, "reason": "graphic content flagged in this window"}
    if set(hit) & HOLD_FLAGS:
        return {**base, "status": "hold_review", "confidence": 0.6, "review_required": True, "reason": "borderline content flagged; needs a human decision"}
    return {**base, "status": "pass", "confidence": round(min(0.97, 0.7 + 0.05 * len(inside)), 2), "review_required": False,
            "reason": "no adult or suggestive content flagged in the analysed frames of this window"}

# ---------- scoring ----------
def source_velocity(info: dict) -> float | None:
    views, up = info.get("view_count"), str(info.get("upload_date") or "")
    if not views or not re.fullmatch(r"\d{8}", up):
        return None
    hours = max(1.0, (datetime.now(timezone.utc) - datetime.strptime(up, "%Y%m%d").replace(tzinfo=timezone.utc)).total_seconds() / 3600)
    return clamp01(math.log10(1 + float(views) / hours) / 4)

def score_event(ev: dict, signals: dict, w: dict[str, float] | None = None) -> dict:
    w = w or weights(); missing, total, comps = [], 0.0, {}
    for k in COMPONENTS:
        comps[k] = clamp01(ev.get(k)); total += w[k] * comps[k]
    support = [signals.get(k) for k in ("audio", "chat", "replay") if signals.get(k) is not None]
    sup = max(support) if support else None
    if sup is None:
        missing.append("support")
    total += w["support"] * (sup or 0.0)
    vel = signals.get("velocity")
    if vel is None:
        missing.append("source_velocity")
    total += w["source_velocity"] * (vel or 0.0)
    comps.update(support=round(sup or 0.0, 3), source_velocity=round(vel or 0.0, 3))
    conf = clamp01(ev.get("confidence"))
    penalty = 0.7 + 0.3 * conf
    return {"components": {k: round(v, 3) for k, v in comps.items()}, "weights": w, "weighted": round(total, 4), "confidence": round(conf, 3),
            "confidence_penalty": round(penalty, 3), "combined": round(total * penalty, 4), "missing_signals": missing}

def window_for_event(ev: dict, duration: float) -> tuple[float, float]:
    s = max(0.0, ev["start_s"] - cfgf("VISUAL_PREROLL_S")); e = min(duration, ev["end_s"] + cfgf("VISUAL_POSTROLL_S"))
    lo, hi = cfgf("VISUAL_MIN_CLIP_S"), cfgf("VISUAL_MAX_CLIP_S")
    if e - s < lo:
        pad = (lo - (e - s)) / 2; s, e = max(0.0, s - pad), min(duration, e + pad)
        if e - s < lo:
            e = min(duration, s + lo)
    if e - s > hi:
        s = e - hi                          # keep the payoff, drop surplus setup
    return round(s, 2), round(e, 2)

# ---------- orchestration ----------
def fingerprint(video: Path) -> str:
    h = hashlib.sha1(str(video.stat().st_size).encode())
    with open(video, "rb") as f:
        h.update(f.read(1 << 20)); f.seek(max(0, video.stat().st_size - (1 << 20))); h.update(f.read(1 << 20))
    keys = ("VISUAL_FRAME_INTERVAL_S", "VISUAL_MAX_FRAMES", "VISUAL_FRAME_MAX_WIDTH", "VISUAL_FRAMES_PER_REQUEST")
    h.update("|".join(cfg(k) for k in keys).encode())
    return h.hexdigest()[:16]

def analyse(video: Path, folder: Path, info: dict, cands: list[dict], duration: float) -> dict:
    out_path = folder / "visual_scenes.json"
    src = {"source_id": info.get("id") or folder.name, "url": info.get("webpage_url"), "title": info.get("title"),
           "creator": info.get("channel") or info.get("uploader"), "duration_s": round(duration, 2)}
    base = {"schema_version": SCHEMA_VERSION, "source": src, "events": []}
    if cfg("VISUAL_ANALYSIS_ENABLED").lower() not in ("1", "true", "yes"):
        return {**base, "analysis": {"status": "disabled"}}
    fp = fingerprint(video)
    if cfg("VISUAL_CACHE_ENABLED").lower() in ("1", "true", "yes") and out_path.exists():
        try:
            cached = json.loads(out_path.read_text())
            if cached.get("analysis", {}).get("fingerprint") == fp and cached["analysis"].get("status") == "complete":
                return cached
        except Exception:
            pass
    t0 = time.time()
    focus = [(c["start"], c["end"]) for c in cands if "start" in c and "end" in c]
    times = plan_timestamps(duration, cfgf("VISUAL_FRAME_INTERVAL_S"), int(cfgf("VISUAL_MAX_FRAMES")), focus)
    frames, failed = extract_frames(video, times)
    res = observe(frames) if frames else {"observations": [], "possible_events": [], "flags": [], "ok_times": [], "providers": set(), "errors": 1, "chunks": 1}
    covered = 1 - res["errors"] / max(res["chunks"], 1)
    status = "complete" if res["ok_times"] and covered >= 0.6 else "unavailable"
    events: list[dict] = []
    if status == "complete":
        merged = text_json(MERGE_SYSTEM, json.dumps({"observations": res["observations"], "possible_events": res["possible_events"], "duration_s": duration})[:24000])
        raw = (merged or {}).get("events") if isinstance(merged, dict) else None
        raw = raw if isinstance(raw, list) else events_from_observations(res["observations"], res["possible_events"])
        norm = [n for n in (normalize_event(e, duration) for e in raw if isinstance(e, dict)) if n]
        events = merge_adjacent(norm, cfgf("VISUAL_MERGE_GAP_S"))
        for i, ev in enumerate(events, 1):
            ev["event_id"] = f"evt-{i:03d}"
            ev["safety"] = window_safety(ev["start_s"], ev["end_s"], res["flags"], res["ok_times"])
    result = {**base, "analysis": {"provider": "+".join(sorted(res["providers"])) or None, "model": wc.env("GROQ_VISION_MODEL"),
              "sampled_frames": len(frames), "failed_frames": failed, "chunks": res["chunks"], "chunk_errors": res["errors"],
              "elapsed_s": round(time.time() - t0, 1), "completed_at": datetime.now(timezone.utc).isoformat(), "status": status,
              "fingerprint": fp, "sample_times": times}, "events": events, "safety_flags_seen": sorted({f for _, f in res["flags"]})}
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result

def _audio_signals(cands: list[dict], s: float, e: float) -> dict:
    sig = {"audio": None, "chat": None, "replay": None}
    for c in cands:
        if c.get("start") is None or c["end"] < s or c["start"] > e:
            continue
        sig["audio"] = max(sig["audio"] or 0.0, clamp01(float(c.get("score") or 0) / 5.0))
        why = " ".join(c.get("why") or [])
        if "chat" in why: sig["chat"] = 0.6
        if "most replayed" in why: sig["replay"] = 0.6
    return sig

def build_candidates(vs: dict, cands: list[dict], duration: float, info: dict) -> tuple[list[dict], list[dict]]:
    status = vs.get("analysis", {}).get("status")
    if status != "complete":                 # never pretend the transcript-only candidates were visually reviewed
        kept = []
        for c in cands:
            c = dict(c); c.update(visual_status=status or "unavailable", needs_vision_review=True); kept.append(c)
        return kept, []
    w, vel = weights(), source_velocity(info); picks, decisions = [], []
    for ev in vs["events"]:
        s, e = window_for_event(ev, duration)
        sig = _audio_signals(cands, s, e); sig["velocity"] = vel
        sc = score_event(ev, sig, w)
        reason = None
        if sc["combined"] < cfgf("VISUAL_MIN_EVENT_SCORE"):
            reason = f"event score {sc['combined']:.2f} is below {cfgf('VISUAL_MIN_EVENT_SCORE'):.2f}"
        elif sc["confidence"] < cfgf("VISUAL_MIN_CONFIDENCE"):
            reason = f"visual confidence {sc['confidence']:.2f} is below {cfgf('VISUAL_MIN_CONFIDENCE'):.2f}"
        words = " ".join(c.get("text", "") for c in cands if c.get("start") is not None and not (c["end"] < s or c["start"] > e))[:300]
        decisions.append({"event_id": ev["event_id"], "start": s, "end": e, "category": ev["primary_category"], "combined": sc["combined"],
                          "selected": reason is None, "reason": reason or "clear event with enough confidence", "safety": ev["safety"]["status"]})
        if reason:
            continue
        picks.append({"start": s, "end": e, "peak": int((ev["evidence_timestamps"] or [(ev["start_s"] + ev["end_s"]) / 2])[len(ev["evidence_timestamps"]) // 2] if ev["evidence_timestamps"] else (ev["start_s"] + ev["end_s"]) / 2),
                      "score": round(sc["combined"] * 10, 2), "why": [f"visual: {ev['primary_category']}"] + [f"{k} {v:.2f}" for k, v in sc["components"].items() if v >= .7][:3],
                      "text": words, "category": ev["primary_category"], "visual_status": "complete", "needs_vision_review": False, "safety": ev["safety"],
                      "visual": {"event_id": ev["event_id"], "event_start_s": ev["start_s"], "event_end_s": ev["end_s"], "primary_category": ev["primary_category"],
                                 "secondary_categories": ev["secondary_categories"], "hook_fact": ev.get("hook_fact"), "summary": ev.get("summary"),
                                 "trigger": ev.get("trigger"), "escalation": ev.get("escalation"), "payoff": ev.get("payoff"),
                                 "viewer_question": ev.get("viewer_question"), "evidence_timestamps": ev["evidence_timestamps"],
                                 "uncertainties": ev["uncertainties"], "components": sc["components"], "combined": sc["combined"],
                                 "missing_signals": sc["missing_signals"], "safety": ev["safety"]}})
    picks.sort(key=lambda c: -c["score"])
    kept: list[dict] = []
    for c in picks:                          # drop overlapping windows, keep the stronger event
        if all(c["end"] <= k["start"] or c["start"] >= k["end"] for k in kept):
            kept.append(c)
    kept = kept[: int(cfgf("VISUAL_MAX_EVENTS_PER_SOURCE"))]
    kept.sort(key=lambda c: c["start"])
    for i, c in enumerate(kept, 1):
        c["n"] = i
    return kept, decisions

def write_report(folder: Path, vs: dict, decisions: list[dict]) -> None:
    fmt = lambda t: f"{int(t // 60)}:{int(t % 60):02d}"
    a = vs.get("analysis", {})
    md = [f"# Visual scenes: {vs['source'].get('title') or folder.name}", f"status: {a.get('status')} | provider: {a.get('provider')} | frames: {a.get('sampled_frames')} | {a.get('elapsed_s')}s", ""]
    if a.get("status") != "complete":
        md.append("Vision analysis was not available, so nothing here was visually reviewed (NEEDS_VISION_REVIEW).")
    for ev in vs.get("events", []):
        d = next((x for x in decisions if x["event_id"] == ev["event_id"]), {})
        md += [f"## {ev['event_id']} {fmt(ev['start_s'])}-{fmt(ev['end_s'])} {ev['primary_category']} | safety {ev['safety']['status']} | " + ("SELECTED" if d.get("selected") else "REJECTED"),
               f"hook fact: {ev.get('hook_fact')}", f"trigger: {ev.get('trigger')} | payoff: {ev.get('payoff')}",
               f"evidence at: {ev['evidence_timestamps']} | score {d.get('combined')} | reason: {d.get('reason')}", ""]
    if not vs.get("events") and a.get("status") == "complete":
        md.append("No distinct visual event found. Skipping this source is a quality decision, not a failure.")
    (folder / "visual_scenes.md").write_text("\n".join(md), encoding="utf-8")

def enrich_candidates(folder: Path, data: dict, info: dict) -> list[dict]:
    """Entry point used by automation/runner.py. Rewrites candidates.json so cut.py --pick sees the merged candidates."""
    cands = data.get("candidates") or []
    duration = float(data.get("duration") or 0)
    video = Path(data["source"])
    original = folder / "candidates_audio.json"
    if not original.exists():
        original.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    vs = analyse(video, folder, info, cands, duration)
    merged, decisions = build_candidates(vs, cands, duration, info)
    write_report(folder, vs, decisions)
    data = dict(data); data["candidates"] = merged
    data["signals"] = {**(data.get("signals") or {}), "visual": vs.get("analysis", {}).get("status")}
    (folder / "candidates.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return merged

"""General-audience safety gate for clip candidates (policy general-audience-1).

filter_candidates(info, candidates) -> (safe, rejected) keeps the old signature. `rejected` is a list of (candidate number, reason).
Statuses: pass, hold_review, reject_adult, reject_other, analysis_failed. analysis_failed is never treated as pass.
SAFETY_MODE=strict (default) fails closed when a candidate has no completed visual review; lenient lets it through but flags
needs_vision_review. High scores and engagement never change an outcome.
"""
from __future__ import annotations
import os, re

POLICY_VERSION = "general-audience-1"
HARD = re.compile(r"\b(nude|nudity|naked|topless|porn\w*|onlyfans|nsfw|sex scene|sexual\w*|strip club|stripper|lap dance|twerk\w*|"
                  r"horny|prostitut\w*|hooker|fetish|erotic)\b", re.I)
SOFT = re.compile(r"\b(sexy|thicc|booty|panties|bikini|cleavage|thirst trap|jiggle|bouncing|sex|seduc\w*|flirt\w*)\b", re.I)

def _mode() -> str:
    return (os.getenv("SAFETY_MODE") or "strict").lower()

def _min_conf() -> float:
    try:
        return float(os.getenv("SAFETY_REVIEW_THRESHOLD") or 0.7)
    except ValueError:
        return 0.7

def _decision(status: str, reason: str, flags=None, conf: float = 0.0, review: bool = False) -> dict:
    return {"status": status, "flags": list(flags or []), "reason": reason, "confidence": conf, "review_required": review, "policy_version": POLICY_VERSION}

def evaluate(info: dict, cand: dict) -> dict:
    """Decide one candidate. Order: source restriction, transcript/title scan, visual verdict for this exact window."""
    info = info or {}
    if (info.get("age_limit") or 0) >= 18 or info.get("age_restricted") or info.get("is_age_restricted"):
        return _decision("hold_review", "source is age-restricted; not bypassed", ["age_restricted"], 1.0, True)
    text = f"{info.get('title') or ''} {cand.get('text') or ''}"
    hard = sorted({m.group(0).lower() for m in HARD.finditer(text)})
    if hard:
        return _decision("reject_adult", "adult wording in title or transcript", ["text:" + h for h in hard], 0.9)
    soft = sorted({m.group(0).lower() for m in SOFT.finditer(text)})
    vis = cand.get("safety") if cand.get("visual_status") == "complete" else None
    if not isinstance(vis, dict) or vis.get("status") not in ("pass", "hold_review", "reject_adult", "reject_other", "analysis_failed"):
        if _mode() == "lenient":
            d = _decision("analysis_failed", "no completed visual review; flagged NEEDS_VISION_REVIEW", [], 0.0, True)
            d["lenient_pass"] = True
            return d
        return _decision("analysis_failed", "no completed visual review for this window; failing closed", [], 0.0, True)
    status = vis["status"]
    if status != "pass":
        return {**vis, "policy_version": POLICY_VERSION}
    if float(vis.get("confidence") or 0) < _min_conf():
        return _decision("hold_review", f"visual safety confidence {vis.get('confidence')} is below {_min_conf()}", vis.get("flags"), float(vis.get("confidence") or 0), True)
    if soft:
        return _decision("hold_review", "suggestive wording in title or transcript with a visual pass; needs a human decision", ["text:" + s for s in soft], 0.6, True)
    return {**vis, "policy_version": POLICY_VERSION}

def filter_candidates(info, candidates):
    safe, rejected = [], []
    for cand in candidates or []:
        d = evaluate(info or {}, cand)
        cand["safety"] = d
        if d["status"] == "pass" or d.get("lenient_pass"):
            cand["needs_vision_review"] = bool(d.get("lenient_pass"))
            safe.append(cand)
        else:
            rejected.append((cand.get("n"), f"{d['status']}: {d['reason']}"))
    return safe, rejected

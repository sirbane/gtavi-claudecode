"""Safety gate for clip candidates (policy safety-2; adult-content restrictions disabled).

filter_candidates(info, candidates) -> (safe, rejected) keeps the old signature. `rejected` is a list of (candidate number, reason).
Statuses: pass, hold_review, reject_other, analysis_failed. Missing visual analysis is never treated as pass.
SAFETY_MODE=strict (default) fails closed when a candidate has no completed visual review; lenient lets it through but flags
needs_vision_review. High scores and engagement never change an outcome.
"""
from __future__ import annotations
import os

POLICY_VERSION = "safety-2"
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
    """Decide one candidate using visual safety for this exact window, without an adult-content exclusion."""
    info = info or {}
    # Adult/18+ source metadata and wording are intentionally not exclusion criteria.
    # Keep the visual safety review for non-adult hazards and uncertain analysis.
    vis = cand.get("safety") if cand.get("visual_status") == "complete" else None
    if not isinstance(vis, dict) or vis.get("status") not in ("pass", "hold_review", "reject_adult", "reject_other", "analysis_failed"):
        if _mode() == "lenient":
            d = _decision("analysis_failed", "no completed visual review; flagged NEEDS_VISION_REVIEW", [], 0.0, True)
            d["lenient_pass"] = True
            return d
        return _decision("analysis_failed", "no completed visual review for this window; failing closed", [], 0.0, True)
    status = vis["status"]
    if status == "reject_adult":
        return _decision("pass", "adult-content restriction disabled; visual review completed", vis.get("flags"), float(vis.get("confidence") or 0))
    if status != "pass":
        return {**vis, "policy_version": POLICY_VERSION}
    if float(vis.get("confidence") or 0) < _min_conf():
        return _decision("hold_review", f"visual safety confidence {vis.get('confidence')} is below {_min_conf()}", vis.get("flags"), float(vis.get("confidence") or 0), True)
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

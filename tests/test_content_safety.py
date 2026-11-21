import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import content_safety as cs

def _c(**kw):
    base = {"n": 1, "text": "", "visual_status": "complete", "safety": {"status": "pass", "confidence": .95, "flags": [], "reason": "ok"}}
    base.update(kw); return base

def test_clean_candidate_passes():
    safe, rej = cs.filter_candidates({"title": "funny npc"}, [_c()])
    assert len(safe) == 1 and not rej

def test_adult_wording_is_not_an_exclusion():
    safe, rej = cs.filter_candidates({"title": "18+ nude scene"}, [_c(text="adult content")])
    assert len(safe) == 1 and not rej

def test_age_restricted_source_is_not_blocked_by_age_metadata():
    safe, rej = cs.filter_candidates({"age_limit": 18, "age_restricted": True}, [_c()])
    assert len(safe) == 1 and not rej

def test_visual_adult_label_does_not_block_but_other_rejections_do():
    safe, rej = cs.filter_candidates({}, [_c(safety={"status": "reject_adult", "reason": "adult scene", "flags": ["adult"], "confidence": .95})])
    assert len(safe) == 1 and not rej

def test_missing_visual_review_fails_closed_in_strict(monkeypatch):
    monkeypatch.delenv("SAFETY_MODE", raising=False)
    safe, rej = cs.filter_candidates({}, [{"n": 1, "text": "hello", "score": 9}])
    assert not safe and rej[0][1].startswith("analysis_failed")

def test_lenient_mode_flags_needs_review(monkeypatch):
    monkeypatch.setenv("SAFETY_MODE", "lenient")
    safe, rej = cs.filter_candidates({}, [{"n": 1, "text": "hello"}])
    assert len(safe) == 1 and safe[0]["needs_vision_review"] is True

def test_visual_hold_and_reject_are_respected():
    for status in ("hold_review", "reject_other", "analysis_failed"):
        safe, rej = cs.filter_candidates({}, [_c(safety={"status": status, "reason": "r", "flags": [], "confidence": .9})])
        assert not safe and rej[0][1].startswith(status)

def test_low_visual_confidence_is_held():
    safe, rej = cs.filter_candidates({}, [_c(safety={"status": "pass", "confidence": .4, "flags": [], "reason": "ok"})])
    assert not safe and rej[0][1].startswith("hold_review")

def test_high_score_never_overrides_safety():
    safe, rej = cs.filter_candidates({}, [_c(score=99, safety={"status": "reject_other", "reason": "r", "flags": [], "confidence": .9})])
    assert not safe

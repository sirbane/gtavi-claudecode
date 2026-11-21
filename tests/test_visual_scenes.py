import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import visual_scenes as vs

def _ev(**kw):
    base = dict(start_s=4, end_s=14, primary_category="unexpected_npc_behaviour", secondary_categories=[], summary="npc reacts at parked car",
                trigger="approach", escalation="second character responds", payoff="unusual reaction", viewer_question="why?", hook_fact="an NPC reacts beside a parked car",
                evidence_timestamps=[4, 7, 10, 14], visual_surprise=.8, event_clarity=.8, novelty=.7, story_arc=.8, payoff_strength=.7,
                context_independence=.85, phone_readability=.8, reaction_strength=.45, social_commentability=.6, confidence=.82, uncertainties=[])
    base.update(kw); return base

def test_plan_timestamps_short_source_covers_whole_clip():
    t = vs.plan_timestamps(30, 2.0, 30)
    assert t == sorted(t) and t[0] < 1 and t[-1] > 28 and len(t) <= 30

def test_plan_timestamps_long_source_is_bounded_and_not_front_loaded():
    t = vs.plan_timestamps(1500, 2.0, 30)
    assert len(t) <= 30 and t[-1] > 1400 and t[0] < 5

def test_focus_windows_add_samples_and_respect_min_gap():
    t = vs.plan_timestamps(600, 2.0, 30, focus=[(100, 120), (400, 420)])
    assert any(100 <= x <= 120 for x in t) and any(400 <= x <= 420 for x in t)
    assert all(b - a >= 0.79 for a, b in zip(t, t[1:])) and len(t) <= 30

def test_zero_duration_returns_nothing():
    assert vs.plan_timestamps(0, 2.0, 30) == []

def test_normalize_clamps_and_rejects():
    ok = vs.normalize_event(_ev(visual_surprise=7, event_clarity="x", end_s=999), 60)
    assert ok["visual_surprise"] == 1.0 and ok["event_clarity"] == 0.0 and ok["end_s"] == 60
    assert vs.normalize_event(_ev(primary_category="made_up"), 60) is None
    assert vs.normalize_event(_ev(start_s=10, end_s=5), 60) is None

def test_merge_adjacent_same_category_only():
    a, b = vs.normalize_event(_ev(start_s=4, end_s=10), 60), vs.normalize_event(_ev(start_s=12, end_s=18), 60)
    c = vs.normalize_event(_ev(start_s=19, end_s=25, primary_category="physics_animation"), 60)
    m = vs.merge_adjacent([a, b, c], 3.0)
    assert len(m) == 2 and m[0]["start_s"] == 4 and m[0]["end_s"] == 18

def test_window_safety_statuses():
    ok_t = [4, 6, 8, 10]
    assert vs.window_safety(4, 10, [], ok_t)["status"] == "pass"
    assert vs.window_safety(4, 10, [(6, "nudity")], ok_t)["status"] == "reject_adult"
    assert vs.window_safety(4, 10, [(6, "sexualized_framing")], ok_t)["status"] == "reject_adult"
    assert vs.window_safety(4, 10, [(6, "suggestive_interaction")], ok_t)["status"] == "hold_review"
    assert vs.window_safety(4, 10, [(6, "graphic_injury")], ok_t)["status"] == "reject_other"
    assert vs.window_safety(4, 10, [], [])["status"] == "analysis_failed"
    assert vs.window_safety(50, 60, [(6, "nudity")], ok_t)["status"] == "analysis_failed"   # unsafe moment elsewhere is not this window

def test_score_components_in_range_and_missing_recorded():
    s = vs.score_event(vs.normalize_event(_ev(), 60), {"audio": None, "chat": None, "replay": None, "velocity": None})
    assert all(0 <= v <= 1 for v in s["components"].values()) and 0 <= s["combined"] <= 1
    assert set(s["missing_signals"]) == {"support", "source_velocity"}

def test_window_for_event_bounds():
    s, e = vs.window_for_event(vs.normalize_event(_ev(start_s=0.5, end_s=3), 60), 60)
    assert 0 <= s < e <= 60 and e - s >= 8
    s, e = vs.window_for_event(vs.normalize_event(_ev(start_s=2, end_s=58), 60), 60)
    assert e - s <= 40.01 and e <= 60

def _complete(events, cands=None):
    return {"analysis": {"status": "complete"}, "events": events, "source": {}}

def test_reference_pattern_quiet_npc_event_is_selected_and_passes_safety():
    import content_safety as cs
    obs = [{"timestamp_s": t, "confidence": .85, "unexpectedness": u} for t, u in ((4, "uncertain"), (7, "likely"), (10, "yes"), (14, "yes"))]
    pe = [{"start_s": 4, "end_s": 14, "category": "unexpected_npc_behaviour", "description": "A character approaches a parked car and another reacts beside the open door",
           "trigger": "approach", "payoff": "an unusual reaction", "confidence": .8}]
    events = [vs.normalize_event(e, 61) for e in vs.events_from_observations(obs, pe)]
    events = vs.merge_adjacent([e for e in events if e], 3.0)
    for i, e in enumerate(events, 1):
        e["event_id"] = f"evt-{i:03d}"; e["safety"] = vs.window_safety(e["start_s"], e["end_s"], [], [4, 7, 10, 14])
    quiet = [{"n": 1, "start": 2, "end": 20, "peak": 5, "score": 0.4, "why": ["overall energy"], "text": ""}]   # quiet audio, no chat
    picks, decisions = vs.build_candidates(_complete(events), quiet, 61, {})
    assert len(picks) == 1 and picks[0]["category"] == "unexpected_npc_behaviour" and decisions[0]["selected"]
    safe, rejected = cs.filter_candidates({"title": "npc moment"}, picks)
    assert len(safe) == 1 and not rejected

def test_generic_loud_moment_does_not_outrank_or_create_candidates():
    loud = [{"n": 1, "start": 5, "end": 25, "peak": 10, "score": 9.0, "why": ["loudness spike", "hype words"], "text": "oh my god"}]
    picks, _ = vs.build_candidates(_complete([]), loud, 60, {})
    assert picks == []

def test_vision_unavailable_marks_needs_review_and_safety_fails_closed():
    import content_safety as cs
    cands = [{"n": 1, "start": 5, "end": 25, "peak": 10, "score": 4.0, "why": ["loudness spike"], "text": ""}]
    picks, _ = vs.build_candidates({"analysis": {"status": "unavailable"}, "events": []}, cands, 60, {})
    assert picks[0]["needs_vision_review"] and picks[0]["visual_status"] == "unavailable"
    safe, rejected = cs.filter_candidates({}, picks)
    assert not safe and "analysis_failed" in rejected[0][1]

def test_suggestive_event_is_held_even_with_top_scores():
    import content_safety as cs
    e = vs.normalize_event(_ev(visual_surprise=1, event_clarity=1, novelty=1, story_arc=1, payoff_strength=1, confidence=1), 60)
    e["event_id"] = "evt-001"; e["safety"] = vs.window_safety(4, 14, [(7, "suggestive_interaction")], [4, 7, 10, 14])
    picks, _ = vs.build_candidates(_complete([e]), [], 60, {})
    safe, rejected = cs.filter_candidates({}, picks)
    assert not safe and "hold_review" in rejected[0][1]

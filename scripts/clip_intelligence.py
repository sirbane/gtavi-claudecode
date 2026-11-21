#!/usr/bin/env python3
"""Crime Scene Six - retention feedback loop.

Commands
  plan <clip_id> <transcript.txt> [--duration S]   AI editing plan saved to DB
  plan-all [--jobs-dir jobs]                       plan every jobs/*/candidates.md not yet planned
  sync                                             auto-register every Reel on the Facebook Page (via Composio)
  register <clip_id> <fb_video_id> [--hook X] [--version A] [--experiment E] [--duration S]
  snapshot                                         Facebook insights at 1h/6h/24h/72h/7d (append-only)
  report                                           per-clip diagnosis + decision (respects MIN_PLAYS)
  compare <experiment_id>                          A/B comparison
  guidelines                                       what worked / failed, fed back into `plan`

Thresholds come from env / .env (see ENV_DEFAULTS). Snapshots are never overwritten.
"""
import os, sys, json, sqlite3, argparse
from pathlib import Path
from datetime import datetime, timezone
import requests

ENV_DEFAULTS = {
    "DB_PATH": "automation/clip_intel.db",
    "GRAPH_VERSION": "v21.0",
    "SNAPSHOT_HOURS": "1,6,24,72,168",
    "MIN_PLAYS": "500",
    "OPEN_SECOND": "5",
    "OPEN_MIN_RETENTION": "0.70",
    "LATE_DROP_MIN": "0.20",
    "FOLLOW_RATE_LOW": "0.001",
    "AVG_RETENTION_LOW": "0.35",
    "METRIC_MAP": json.dumps({
        "plays": "blue_reels_play_count",
        "avg_watch_s": "post_video_avg_time_watched",
        "total_watch_ms": "post_video_view_time",
        "follows": "post_video_followers",
        "retention": "post_video_retention_graph",
    }),
}

def env(k):
    if os.getenv(k):
        return os.getenv(k)
    if os.path.exists(".env"):
        for line in open(".env"):
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip().strip('"')
    return ENV_DEFAULTS.get(k)

def db():
    Path(env("DB_PATH")).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(env("DB_PATH"))
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS clips(
      clip_id TEXT PRIMARY KEY, label TEXT, fb_video_id TEXT, hook_type TEXT, duration REAL,
      edit_version TEXT, experiment_id TEXT, published_at TEXT, plan_json TEXT);
    CREATE TABLE IF NOT EXISTS snapshots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT, taken_at TEXT, hours_after INTEGER,
      plays INTEGER, replays INTEGER, avg_watch_s REAL, total_watch_s REAL, follows INTEGER,
      retention_json TEXT);
    """)
    return c

PLAN_SYSTEM = """You are a short-form video editor for a GTA 6 clips page. Given clip context and the guidelines
learned from past Facebook retention data, return ONLY JSON:
{"timeline":[{"start":0,"end":0,"label":"hook|context|escalation|payoff|dead_air","note":""}],
 "first_3s":"what happens","payoff_second":0,
 "edit_plan":["ordered instructions"],"keep_original_audio":true,
 "end_after_payoff":true,"suggested_duration":0,"hook_type":"short label"}
Let the story decide the length. Never invent footage you cannot infer from the input."""

def llm(system, user):
    """Talks to the Kaggle model server only."""
    sys.path.insert(0, str(Path(__file__).parent))
    import watch_and_caption as wc
    txt = wc.kaggle_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)
    return json.loads(txt)

def make_plan(c, clip_id, text, duration=None):
    plan = llm(PLAN_SYSTEM, f"GUIDELINES FROM DATA:\n{guidelines_text()}\n\nCLIP ({duration or '?'}s):\n{text[:6000]}")
    c.execute("INSERT OR IGNORE INTO clips(clip_id,duration) VALUES(?,?)", (clip_id, duration))
    c.execute("UPDATE clips SET plan_json=?, hook_type=COALESCE(hook_type,?) WHERE clip_id=?",
              (json.dumps(plan), plan.get("hook_type"), clip_id))
    c.commit()
    return plan

def cmd_plan(a):
    plan = make_plan(db(), a.clip_id, open(a.transcript).read(), a.duration)
    print(json.dumps(plan, indent=2))  # the renderer applies edit_plan to a COPY; the original clip stays untouched

def cmd_plan_all(a):
    if not env("KAGGLE_OLLAMA_URL"):
        print("KAGGLE_OLLAMA_URL not set - skipping edit planning"); return
    c = db(); out = []
    for f in sorted(Path(a.jobs_dir).glob("*/candidates.md")):
        cid = f.parent.name
        row = c.execute("SELECT plan_json FROM clips WHERE clip_id=?", (cid,)).fetchone()
        if row and row["plan_json"]:
            continue
        try:
            plan = make_plan(c, cid, f.read_text(errors="replace"))
        except Exception as e:
            print("plan failed", cid, e); continue
        out.append(f"## {cid}\n- hook: {plan.get('hook_type')}  | first 3s: {plan.get('first_3s')}\n"
                   f"- payoff at: {plan.get('payoff_second')}s | suggested length: {plan.get('suggested_duration')}s\n"
                   + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan.get("edit_plan", []))) + "\n")
        print("planned", cid)
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/edit_plans.md").write_text("\n".join(out) or "No new clips to plan.\n")

def cmd_register(a):
    c = db()
    c.execute("""INSERT INTO clips(clip_id,fb_video_id,hook_type,duration,edit_version,experiment_id,published_at)
                 VALUES(?,?,?,?,?,?,?) ON CONFLICT(clip_id) DO UPDATE SET fb_video_id=excluded.fb_video_id,
                 hook_type=COALESCE(excluded.hook_type,hook_type), duration=COALESCE(excluded.duration,duration),
                 edit_version=COALESCE(excluded.edit_version,edit_version),
                 experiment_id=COALESCE(excluded.experiment_id,experiment_id), published_at=excluded.published_at""",
              (a.clip_id, a.fb_video_id, a.hook, a.duration, a.version, a.experiment, datetime.now(timezone.utc).isoformat()))
    c.commit()

COMPOSIO_BASE = "https://backend.composio.dev/api/v3.1"
_PAGE = {}

def _ch():
    return {"x-api-key": env("COMPOSIO_API_KEY"), "Content-Type": "application/json"}

def _exec(acc, slug, args):
    r = requests.post(f"{COMPOSIO_BASE}/tools/execute/{slug}", headers=_ch(), timeout=120,
                      json={"connected_account_id": acc["id"], "user_id": acc.get("user_id"),
                            "version": "latest", "arguments": args})
    r.raise_for_status()
    d = r.json()
    if d.get("successful") is False:
        raise RuntimeError(d.get("error") or d)
    d = d.get("data") or {}
    return d.get("data", d) if isinstance(d, dict) else d

def fb_page():
    """Find the Facebook Page through the Composio connection; its page token never needs a Meta app."""
    if _PAGE:
        return _PAGE["acc"], _PAGE["id"], _PAGE["token"]
    name = (env("FACEBOOK_PAGE_NAME") or "CrimeScene VI").lower()
    r = requests.get(f"{COMPOSIO_BASE}/connected_accounts", headers=_ch(), params={"limit": 100}, timeout=45)
    r.raise_for_status()
    for acc in r.json().get("items") or []:
        if str((acc.get("toolkit") or {}).get("slug", "")).lower() != "facebook" or str(acc.get("status", "")).upper() != "ACTIVE":
            continue
        try:
            for pg in _exec(acc, "FACEBOOK_LIST_MANAGED_PAGES", {"limit": 100, "fields": "id,name,access_token"}):
                if str(pg.get("name", "")).strip().lower() == name and pg.get("access_token"):
                    _PAGE.update(acc=acc, id=str(pg["id"]), token=pg["access_token"])
                    return acc, _PAGE["id"], _PAGE["token"]
        except Exception as e:
            print("facebook connection check:", e)
    raise RuntimeError("Facebook Page not found through Composio connections")

def fetch_insights(video_id):
    """Graph API video_insights using the page token Composio already holds. Metric names are in METRIC_MAP."""
    mm = json.loads(env("METRIC_MAP"))
    _, _, token = fb_page()
    r = requests.get(f"https://graph.facebook.com/{env('GRAPH_VERSION')}/{video_id}/video_insights",
                     params={"metric": ",".join(mm.values()), "access_token": token}, timeout=60)
    r.raise_for_status()
    got = {d["name"]: (d["values"][0]["value"] if d.get("values") else None) for d in r.json().get("data", [])}
    out = {k: got.get(v) for k, v in mm.items()}
    if out.get("avg_watch_s") is not None:
        out["avg_watch_s"] = out["avg_watch_s"] / 1000.0   # Facebook reports milliseconds
    return out

def cmd_sync(_a=None):
    """Register every Reel on the Page automatically (no manual register step)."""
    if not env("COMPOSIO_API_KEY"):
        print("COMPOSIO_API_KEY not set - skipping sync"); return
    acc, page_id, _ = fb_page()
    vids = _exec(acc, "FACEBOOK_GET_PAGE_VIDEOS", {"page_id": page_id, "limit": 50, "fields": "id,created_time,description,length"})
    c = db(); new = 0
    for v in vids if isinstance(vids, list) else []:
        cid = f"fb_{v['id']}"
        if c.execute("SELECT 1 FROM clips WHERE clip_id=?", (cid,)).fetchone():
            continue
        ts = datetime.strptime(v["created_time"], "%Y-%m-%dT%H:%M:%S%z").isoformat()
        label = ((v.get("description") or "").split("\n")[0])[:60]
        c.execute("INSERT INTO clips(clip_id,label,fb_video_id,duration,published_at) VALUES(?,?,?,?,?)",
                  (cid, label, v["id"], v.get("length"), ts))
        new += 1
    c.commit()
    print(f"synced {new} new Page videos")

def normalize_curve(raw):
    if not raw:
        return {}
    items = raw.items() if isinstance(raw, dict) else enumerate(raw)
    cur = {int(float(k)): float(v) for k, v in items if v is not None}
    if cur and max(cur.values()) > 1.5:
        cur = {k: v / 100 for k, v in cur.items()}
    return cur

def cmd_snapshot(_a=None):
    if not env("COMPOSIO_API_KEY"):
        print("COMPOSIO_API_KEY not set - skipping Facebook snapshots"); return
    c = db(); now = datetime.now(timezone.utc)
    marks = [int(x) for x in env("SNAPSHOT_HOURS").split(",")]
    for clip in c.execute("SELECT * FROM clips WHERE fb_video_id IS NOT NULL AND published_at IS NOT NULL").fetchall():
        age = (now - datetime.fromisoformat(clip["published_at"])).total_seconds() / 3600
        done = {r[0] for r in c.execute("SELECT hours_after FROM snapshots WHERE clip_id=?", (clip["clip_id"],))}
        if done:
            done |= {m for m in marks if m <= max(done)}   # a later snapshot already covers earlier marks
        due = [m for m in marks if age >= m and m not in done]
        if not due:
            continue
        try:
            m = fetch_insights(clip["fb_video_id"])
        except Exception as e:
            print("skip", clip["clip_id"], e); continue
        c.execute("""INSERT INTO snapshots(clip_id,taken_at,hours_after,plays,avg_watch_s,total_watch_s,follows,retention_json)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (clip["clip_id"], now.isoformat(), max(due), m.get("plays"), m.get("avg_watch_s"),
                   (m.get("total_watch_ms") or 0) / 1000, m.get("follows"), json.dumps(normalize_curve(m.get("retention")))))
        print("snapshot", clip["clip_id"], "at", max(due), "h")
    c.commit()

def latest(c, clip_id):
    return c.execute("SELECT * FROM snapshots WHERE clip_id=? ORDER BY id DESC LIMIT 1", (clip_id,)).fetchone()

def analyse(clip, snap):
    curve = {int(k): v for k, v in json.loads(snap["retention_json"] or "{}").items()}
    dur = clip["duration"] or (max(curve) if curve else 0)
    ret_open = curve.get(int(env("OPEN_SECOND")))
    avg_ret = (snap["avg_watch_s"] / dur) if dur and snap["avg_watch_s"] else None
    drop_s, drop = None, 0.0
    for s in sorted(curve):
        if s - 1 in curve and curve[s - 1] - curve[s] > drop:
            drop, drop_s = curve[s - 1] - curve[s], s
    return dict(plays=snap["plays"] or 0, ret_open=ret_open, avg_ret=avg_ret, drop_s=drop_s, drop=drop, dur=dur,
                follow_rate=(snap["follows"] or 0) / max(snap["plays"] or 1, 1))

def decide(m):
    if m["plays"] < int(env("MIN_PLAYS")):
        return "INSUFFICIENT DATA - keep distributing, no label yet"
    out = []
    weak_open = m["ret_open"] is not None and m["ret_open"] < float(env("OPEN_MIN_RETENTION"))
    late = m["drop"] >= float(env("LATE_DROP_MIN")) and m["dur"] and m["drop_s"] and m["drop_s"] > m["dur"] * 0.5
    if weak_open and not late:
        out.append("Weak opening: test moving the strongest moment to the start")
    if late and not weak_open:
        out.append(f"Strong opening, collapse near {m['drop_s']}s: inspect the ending, cut right after the payoff")
    if weak_open and late:
        out.append("Opening and ending both lose viewers: deprioritise, review source footage")
    if m["avg_ret"] is not None and m["avg_ret"] < float(env("AVG_RETENTION_LOW")):
        out.append("Low average retention: lower priority")
    if m["follow_rate"] < float(env("FOLLOW_RATE_LOW")) and not out:
        out.append("Retention fine, few follows: add a reason to follow")
    return "; ".join(out) or "Healthy: keep this structure and test variants"

def pct(x):
    return "n/a" if x is None else f"{x*100:.1f}%"

def cmd_report(_a=None):
    c = db(); n = 0
    for clip in c.execute("SELECT * FROM clips").fetchall():
        s = latest(c, clip["clip_id"])
        if not s:
            continue
        n += 1; m = analyse(clip, s)
        print(f"{(clip['label'] or clip['clip_id'])[:28]:<28} plays={m['plays']:<6} open@{env('OPEN_SECOND')}s={pct(m['ret_open'])} "
              f"avg={pct(m['avg_ret'])} bigdrop={m['drop_s']}s(-{m['drop']*100:.0f}%)\n   -> {decide(m)}")
    if not n:
        print("No clips with Facebook snapshots yet. Register published Reels with: register <clip_id> <fb_video_id>")

def guidelines_text():
    c = db(); good, bad = [], []
    for clip in c.execute("SELECT * FROM clips").fetchall():
        s = latest(c, clip["clip_id"])
        if not s or (s["plays"] or 0) < int(env("MIN_PLAYS")):
            continue
        m = analyse(clip, s)
        (good if (m["avg_ret"] or 0) >= 0.6 else bad if (m["avg_ret"] or 1) < float(env("AVG_RETENTION_LOW")) else []).append(
            f"{clip['hook_type'] or 'unlabelled'} ({clip['duration']}s, avg {pct(m['avg_ret'])})")
    return f"WORKED: {good or 'not enough data yet'}\nFAILED: {bad or 'not enough data yet'}\nThese are tendencies, not rules."

def cmd_guidelines(_a=None):
    print(guidelines_text())

def cmd_compare(a):
    c = db(); rows = []
    for clip in c.execute("SELECT * FROM clips WHERE experiment_id=?", (a.experiment_id,)).fetchall():
        s = latest(c, clip["clip_id"])
        if s:
            rows.append((clip["edit_version"], analyse(clip, s)))
    for v, m in rows:
        print(f"version {v}: plays={m['plays']} open={pct(m['ret_open'])} avg={pct(m['avg_ret'])}")
    if len(rows) >= 2 and all(m["plays"] >= int(env("MIN_PLAYS")) for _, m in rows):
        print("leader:", max(rows, key=lambda r: r[1]["avg_ret"] or 0)[0], "(by average retention)")
    else:
        print("Not enough plays on every version yet - no winner declared")

def main():
    p = argparse.ArgumentParser(); sp = p.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("plan"); x.add_argument("clip_id"); x.add_argument("transcript"); x.add_argument("--duration", type=float)
    x = sp.add_parser("plan-all"); x.add_argument("--jobs-dir", default="jobs")
    x = sp.add_parser("register"); x.add_argument("clip_id"); x.add_argument("fb_video_id")
    x.add_argument("--hook"); x.add_argument("--version"); x.add_argument("--experiment"); x.add_argument("--duration", type=float)
    sp.add_parser("snapshot"); sp.add_parser("sync"); sp.add_parser("report"); sp.add_parser("guidelines")
    x = sp.add_parser("compare"); x.add_argument("experiment_id")
    a = p.parse_args()
    {"plan": cmd_plan, "plan-all": cmd_plan_all, "register": cmd_register, "sync": cmd_sync, "snapshot": cmd_snapshot,
     "report": cmd_report, "guidelines": cmd_guidelines, "compare": cmd_compare}[a.cmd](a)

if __name__ == "__main__":
    main()

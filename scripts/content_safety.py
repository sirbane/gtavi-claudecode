"""Conservative brand-safety filter for Crime Scene Six clip candidates."""
import re
_SOURCE_FLAGS = re.compile(r"(?:^|\W)18\s*\+(?:\W|$)|\b(?:age[- ]restricted|adult[- ]only|mature audience|nsfw|not safe for work|uncensored)\b", re.I)
_ADULT_OR_GORE = re.compile(r"\b(?:nude|nudity|naked|topless|sex scene|sexual(?:ly)? explicit|porn(?:ography)?|xxx|genitals?|penis|vagina|nipples?|rape|prostitut\w*|strip(?:per|\s*club)|hooker|dismember\w*|decapitat\w*|behead\w*|mutilat\w*|gore|guts|viscera|corpse|body parts|graphic blood|fuck\w*|motherfuck\w*|shit\w*|bitch\w*|cunt\w*|dick\w*|asshole\w*)\b", re.I)
_SLURS = re.compile(r"\b(?:nigg\w*|n[i1]gg?a\w*|f[a@]gg?\w*|retard\w*|tranny\w*|chink\w*|spic\w*|kike\w*)\b", re.I)

def source_rejection(info):
    info = info or {}
    age_limit = info.get("age_limit")
    try:
        if age_limit is not None and int(age_limit) >= 18:
            return f"source metadata age_limit={age_limit}"
    except (TypeError, ValueError):
        pass
    if info.get("age_restricted") is True or info.get("is_age_restricted") is True:
        return "source metadata marks this video age-restricted"
    labels = " ".join(str(info.get(k) or "") for k in ("title", "description", "availability"))
    if _SOURCE_FLAGS.search(labels):
        return "source title/description is labelled 18+, adult, NSFW or uncensored"
    return ""

def candidate_rejection(candidate):
    text = str((candidate or {}).get("text") or "")
    if not text.strip():
        return "no transcript text available for content screening"
    match = _ADULT_OR_GORE.search(text) or _SLURS.search(text)
    if match:
        return f"transcript contains restricted language/content ({match.group(0)[:32]!r})"
    return ""

def filter_candidates(info, candidates):
    reason = source_rejection(info)
    if reason:
        return [], [(c.get("n", "?"), reason) for c in candidates]
    safe, rejected = [], []
    for candidate in candidates:
        reason = candidate_rejection(candidate)
        if reason:
            rejected.append((candidate.get("n", "?"), reason))
        else:
            safe.append(candidate)
    return safe, rejected

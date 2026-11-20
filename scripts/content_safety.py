"""Compatibility shim: content screening is disabled in the clip workflow.

Keep this module so existing runner imports remain valid, but do not reject
source videos or transcript candidates here.
"""


def filter_candidates(info, candidates):
    """Return every candidate unchanged; no content filter is applied."""
    return list(candidates or []), []

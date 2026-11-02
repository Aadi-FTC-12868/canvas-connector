"""Intent-bundle tools — composed operations at the user-intent level.

These sit on top of the raw endpoint layer. Each tool bundles multiple
API calls into one rich, context-laden return. An agent calling
`whats_new()` gets everything a student would see at a glance — not a
paginated JSON dump that requires another three calls to interpret.

Design rules:
    - No LLM calls. Intent bundles are deterministic compositions.
      Templates for text summaries; leave narrative framing to the caller.
    - Typed returns (Pydantic). Agents can reason about structure.
    - Each tool is a single function call; multi-step orchestration stays
      inside the library where we can test it.
    - Failures in one sub-call do not kill the whole bundle — missing
      data is logged and the return carries a `warnings` list.

Reference: docs/capability-map.md §Tier 3 — user-intent bundled tools.
"""
from __future__ import annotations

from ._audits import missing_work_audit
from ._digests import catch_me_up_on_course, whats_new
from ._models import (
    AnnouncementBrief,
    CourseCatchup,
    CrossCourseDigest,
    MissingInCourse,
    MissingWorkAudit,
    RecentFeedback,
    UpcomingItem,
)

__all__ = [
    "whats_new",
    "catch_me_up_on_course",
    "missing_work_audit",
    "CourseCatchup",
    "CrossCourseDigest",
    "MissingWorkAudit",
    "MissingInCourse",
    "UpcomingItem",
    "AnnouncementBrief",
    "RecentFeedback",
]

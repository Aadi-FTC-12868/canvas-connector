"""canvas-mcp-v2 — MCP server wrapping canvas-py.

Exposes Canvas LMS capabilities over the Model Context Protocol so Claude
Code, your 24/7 agent, or any other MCP client can call into your personal
Canvas instance via typed tools.

Design posture:
    - Reads: fully exposed.
    - Intent bundles: fully exposed (whats_new, catch_me_up_on_course, etc).
    - Safe writes (planner notes, planner overrides): exposed.
    - Dangerous writes (submissions, discussion posts, inbox messages):
      NOT exposed by default. Adding them requires an explicit confirmation
      gate because they are irreversible.
"""
from __future__ import annotations

__version__ = "0.1.0dev0"

"""LABEL_ROUTES of ForgeHands direct mode: which agent profile a Forgejo label starts, and its limits.

The profiles are created by the agents step (planner, worker, worker-strong, reviewer); once it has their ids it passes them in, so the
routes name the ids and not the profile names. setup.sh writes the same routes when it selects the agents profile.
"""
from __future__ import annotations
import json

# label -> (agent profile name, tier, max_iterations)
LABELS = (
    ("ai:plan", "planner", "planner", 40),
    ("ai:ready", "worker", "worker", 120),
    ("ai:escalate", "worker-strong", "worker-strong", 120),
    ("ai:review", "reviewer", "reviewer", 60),
)


def label_routes(profile_ids: dict[str, str] | None = None) -> str:
    """The LABEL_ROUTES value; a profile the caller has no id for keeps its name."""
    ids = profile_ids or {}
    routes = {label: {"profile": ids.get(name, name), "tier": tier, "max_iterations": iterations}
              for label, name, tier, iterations in LABELS}
    return json.dumps(routes, separators=(",", ":"))

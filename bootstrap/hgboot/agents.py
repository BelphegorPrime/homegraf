"""LABEL_ROUTES of ForgeHands direct mode: which agent profile a Forgejo label starts, and its limits.

OpenHands takes the agent profile as an id, not a name (`POST /api/conversations` rejects a name with HTTP 422), so `label_routes()` takes
the ids that `GET /api/agent-profiles` lists. The agents step does not create the profiles yet (#25), so nothing calls it: setup.sh and the
wizard template leave `LABEL_ROUTES` empty and direct mode off until the ids exist.
"""
from __future__ import annotations
import json

# label -> (agent profile name, tier, max_iterations)
LABELS = (
    ("ai:plan", "planner", "planner", 80),
    ("ai:ready", "worker", "worker", 120),
    ("ai:escalate", "worker-strong", "worker-strong", 120),
    ("ai:review", "reviewer", "reviewer", 60),
)


def label_routes(profile_ids: dict[str, str]) -> str:
    """The LABEL_ROUTES value built from the profile ids; an id for every profile is required, a name would fail every run."""
    missing = sorted({name for _, name, _, _ in LABELS if not profile_ids.get(name)})
    if missing:
        raise ValueError(f"no agent profile id for {', '.join(missing)}")
    routes = {label: {"profile": profile_ids[name], "tier": tier, "max_iterations": iterations}
              for label, name, tier, iterations in LABELS}
    return json.dumps(routes, separators=(",", ":"))

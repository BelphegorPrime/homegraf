"""LABEL_ROUTES of ForgeHands direct mode: which agent profile a Forgejo label or event starts, and its limits.

OpenHands takes the agent profile as an id, not a name (`POST /api/conversations` rejects a name with HTTP 422), so `label_routes()` takes
the ids that `GET /api/agent-profiles` lists. The agents step does not create the profiles yet (#25), so nothing calls it: setup.sh and the
wizard template leave `LABEL_ROUTES` empty and direct mode off until the ids exist.

Four routes start on a label. `ai:changes` starts on a pull request label, and the keys `ci:failure`, `ci:success` and `epic:done` are
synthetic: they start on the CI and epic events, so they need no repository label.
"""
from __future__ import annotations
import json

# label -> (agent profile name, tier, max_iterations, the event that fires it; empty for a label)
LABELS = (
    ("ai:plan", "planner", "planner", 80, ""),
    ("ai:ready", "worker", "worker", 120, ""),
    ("ai:escalate", "worker-strong", "worker-strong", 120, ""),
    ("ai:review", "reviewer", "reviewer", 60, ""),
    ("ai:changes", "worker", "worker", 120, "pull_request"),
    ("ci:failure", "worker", "worker", 120, "ci_failure"),
    ("ci:success", "reviewer", "reviewer", 60, "ci_success"),
    ("epic:done", "reviewer", "reviewer", 60, "epic_done"),
)


def label_routes(profile_ids: dict[str, str]) -> str:
    """The LABEL_ROUTES value built from the profile ids; an id for every profile is required, a name would fail every run."""
    for name, profile_id in profile_ids.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("an agent profile name is blank")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError(f"the agent profile id for {name} is blank")
    missing = sorted({name for _, name, _, _, _ in LABELS if not profile_ids.get(name)})
    if missing:
        raise ValueError(f"no agent profile id for {', '.join(missing)}")
    routes = {}
    for label, name, tier, iterations, event in LABELS:
        route = {"profile": profile_ids[name], "tier": tier, "max_iterations": iterations}
        if event:
            route["on"] = event
        routes[label] = route
    return json.dumps(routes, separators=(",", ":"))

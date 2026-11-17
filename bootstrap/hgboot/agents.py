"""LABEL_ROUTES of ForgeHands direct mode: which agent profile a Forgejo label or event starts, and its limits.

OpenHands takes the agent profile as an id, not a name (`POST /api/conversations` rejects a name with HTTP 422), so `label_routes()` takes
the ids that `GET /api/agent-profiles` lists. The factory reconciles the profiles first and publishes these routes in its runtime file.

The routes use the reference installation's tiers: `cheap` for the worker routes, `smart` for the planner, the reviewers, the escalation
target and the conflict round, and `free` for the fallback routes. `ai:changes` and `ci:failure` escalate their last round to
`ai:escalate`; while their tier is out of quota they fall back to `ai:changes-free` and `ci:failure-free`, and `ai:ready` to
`ai:ready-free`. Those routes need no label: nobody triggers them from an issue, so they exist only when the `worker-free` profile has an
id.

Four routes start on a label. `ai:changes` starts on a pull request label, and the keys `ci:failure`, `ci:success`, `epic:done`,
`pr:conflict` and `sensitive:review` are synthetic: they start on the CI, epic, branch-conflict and protected-path events, so they need
no repository label.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
import json

FALLBACK_PROFILE = "worker-free"
ROUND_KINDS = ("pull_request", "ci_failure", "conflict")
# The AGENT_SECRETS entry that carries the security reviewer's own forge token; the route names it,
# never the value, and ForgeHands hands it to the security review conversations only.
SECURITY_TOKEN_SECRET = "FORGEJO_SECURITY_TOKEN"


@dataclass(frozen=True)
class Route:
    profile: str
    tier: str
    max_iterations: int | None = None
    on: str = ""
    fallback_to: str = ""
    escalate_to: str = ""


# route -> agent profile name, tier, iteration limit, event that fires it (empty for a label) and the routes it falls back or escalates to
ROUTES = (
    ("ai:plan", Route("planner", "smart", 80)),
    ("ai:ready", Route("worker", "cheap", 120, fallback_to="ai:ready-free")),
    ("ai:ready-free", Route(FALLBACK_PROFILE, "free")),
    ("ai:escalate", Route("worker-strong", "smart", 120)),
    ("ai:review", Route("reviewer", "smart", 60)),
    ("ai:changes", Route("worker", "cheap", 120, on="pull_request", fallback_to="ai:changes-free",
                         escalate_to="ai:escalate")),
    ("ai:changes-free", Route(FALLBACK_PROFILE, "free")),
    ("ci:failure", Route("worker", "cheap", 120, on="ci_failure", fallback_to="ci:failure-free",
                         escalate_to="ai:escalate")),
    ("ci:failure-free", Route(FALLBACK_PROFILE, "free")),
    ("ci:success", Route("reviewer", "smart", 60, on="ci_success")),
    ("epic:done", Route("reviewer", "smart", 60, on="epic_done")),
    ("pr:conflict", Route("worker-strong", "smart", 120, on="conflict")),
    ("sensitive:review", Route("sensitive-reviewer", "smart", 60, on="sensitive_review")),
)
FREE_ROUTE_LABELS = frozenset(label for label, route in ROUTES if route.profile == FALLBACK_PROFILE)


def _select(profile_ids: dict[str, str]) -> tuple[tuple[str, Route], ...]:
    """The routes that can exist: the fallback routes need the `worker-free` profile, the others every profile they name."""
    has_free = bool(profile_ids.get(FALLBACK_PROFILE))
    selected = []
    for label, route in ROUTES:
        if route.profile == FALLBACK_PROFILE and not has_free:
            continue
        selected.append((label, route if has_free else replace(route, fallback_to="")))
    return tuple(selected)


def _validate(routes: dict[str, dict]) -> None:
    """The checks ForgeHands makes at start: every target exists, one fallback hop, escalation only on a round route."""
    for label, route in routes.items():
        target = route.get("fallback_to")
        if target is None:
            continue
        if target == label or target not in routes:
            raise ValueError(f"{label}: fallback_to must name another route")
        if "fallback_to" in routes[target]:
            raise ValueError(f"{label}: fallback_to must name a route without a fallback_to of its own")
    for label, route in routes.items():
        target = route.get("escalate_to")
        if target is None:
            continue
        if route.get("on") not in ROUND_KINDS:
            raise ValueError(f"{label}: escalate_to only applies to a round route")
        if target == label or target not in routes:
            raise ValueError(f"{label}: escalate_to must name another route")


def label_routes(profile_ids: dict[str, str], security_token: str = "") -> str:
    """The LABEL_ROUTES value built from the profile ids; an id for every profile the routes use is required, a name would fail every run.

    A configured ``security_token`` makes the security review route name its own secret, so that the
    reviewer writes the second key as a forge user the worker's token cannot sign as.
    """
    for name, profile_id in profile_ids.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("an agent profile name is blank")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError(f"the agent profile id for {name} is blank")
    selected = _select(profile_ids)
    missing = sorted({route.profile for _, route in selected if not profile_ids.get(route.profile)})
    if missing:
        raise ValueError(f"no agent profile id for {', '.join(missing)}")
    routes = {}
    for label, route in selected:
        entry = {"profile": profile_ids[route.profile], "tier": route.tier}
        if route.max_iterations is not None:
            entry["max_iterations"] = route.max_iterations
        if route.on:
            entry["on"] = route.on
        if route.fallback_to:
            entry["fallback_to"] = route.fallback_to
        if route.escalate_to:
            entry["escalate_to"] = route.escalate_to
        if security_token and route.on == "sensitive_review":
            entry["forgejo_token_secret"] = SECURITY_TOKEN_SECRET
        routes[label] = entry
    _validate(routes)
    return json.dumps(routes, separators=(",", ":"))

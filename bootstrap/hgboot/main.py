"""python -m hgboot [run | show infisical-password]: configure the stack, in order, and say what is left."""
from __future__ import annotations

import os
import sys

from . import handoff, seed
from .adguard import AdGuard
from .config import Config, ConfigError
from .forgejo import Forgejo
from .http import Unreachable
from .infisical import Infisical
from .ingress import Ingress
from .komodo import Komodo
from .npm import Npm
from .renovate import Renovate
from .state import State, write_env_file
from .steps import StepError, log, warn


def certificates(env=os.environ) -> int:
    """The private CA and ingressctl, after the rest: ingressctl checks every upstream, and homegraf, the last service, has to be up.
    A failure is a warning, not an error: the proxy hosts work over plain HTTP, and nothing may stop the stack because of it."""
    try:
        cfg = Config.from_env(env)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    state = State(f"{cfg.state_dir}/state.json")
    forgejo, komodo, npm = Forgejo(cfg, state), Komodo(cfg, state), Npm(cfg, state)
    try:
        forgejo.wait(lambda: forgejo.http("GET", cfg.forgejo_url + "/api/healthz").ok, "Forgejo", timeout=60)
        komodo.connect()
        npm.connect()
        Ingress(cfg, state, forgejo, komodo, npm).run()
    except (StepError, Unreachable, TimeoutError, ValueError, OSError) as e:
        warn("ingress", f"certificates are not set up: {e}\n  The proxy hosts work over plain HTTP. Fix the cause and run it again: docker compose up -d certificates")
        print(handoff.certificates_summary(cfg, False), flush=True)
        return 0
    print(handoff.certificates_summary(cfg, True), flush=True)
    return 0


def run(env=os.environ) -> int:
    try:
        cfg = Config.from_env(env)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    state = State(f"{cfg.state_dir}/state.json")
    try:
        files = seed.collect(seed.seed_dir(cfg), seed.substitutions(cfg, "Local"))
        if not files:
            raise StepError("the image contains no seed (neither /seed nor /seed-stub): rebuild it from the repository root with "
                            "docker build -f bootstrap/Dockerfile .")
        forgejo = Forgejo(cfg, state)
        forgejo.run(files)
        if "renovate" in cfg.profiles:
            Renovate(cfg, state, forgejo).run()
        komodo = Komodo(cfg, state)
        server = komodo.run(state.get("forgejo", "token"))
        if server != "Local":
            log("komodo", f"note: the server is called {server!r}; the seed said 'Local' where it names the server")
        Npm(cfg, state).run()
        Infisical(cfg, state).run()
        if cfg.dns_mode == "adguard":
            AdGuard(cfg, state).run()
        write_env_file(f"{cfg.state_dir}/homegraf.env", handoff.homegraf_env(cfg, state))
        log("handoff", f"homegraf's settings written to {cfg.state_dir}/homegraf.env (homegraf reads it at start)")
    except (StepError, Unreachable, TimeoutError, ValueError) as e:
        print(f"\nerror: {e}\nNothing that is done is undone: fix the cause and run it again (docker compose up -d bootstrap).", file=sys.stderr)
        return 1
    print(handoff.summary(cfg, state, handoff.next_steps(cfg)), flush=True)
    return 0


def show(what: str, env=os.environ) -> int:
    state = State(f"{env.get('STATE_DIR') or '/state'}/state.json")
    if what == "infisical-password":
        password = state.get("infisical", "admin_password")
        print(password if password else "Infisical uses ADMIN_PASSWORD from .env")
        return 0
    print("usage: show infisical-password", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["show"]:
        return show(argv[1] if len(argv) > 1 else "")
    if argv[:1] == ["certificates"]:
        return certificates()
    if argv and argv[0] != "run":
        print("usage: python -m hgboot [run | certificates | show infisical-password]", file=sys.stderr)
        return 2
    return run()

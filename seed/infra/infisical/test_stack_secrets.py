"""Tests für stack_secrets.py mit Fakes für Infisical v0.167.2 und Komodo 2.3.x (pytest infra/infisical)."""
import pathlib
import re
import sys
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import stack_secrets as ss  # noqa: E402

RUN_DIR = "/srv/periphery/data/komodo/repos/infrastructure-worker/stacks/searxng"
STACK = {"id": "searxng", "path": "stacks/searxng", "server": "worker-1", "komodo_stack": "searxng",
         "secrets": "infisical"}


class FakeInfisical:
    org_id = "org-1"

    def __init__(self):
        self.projects, self.folders, self.secrets, self.identities = [], set(), {}, {}
        self.ua, self.client_secrets, self.members = {}, {}, {}
        self.writes = []

    def call(self, method, path, body=None, auth=True):
        url = urllib.parse.urlparse(path)
        q = dict(urllib.parse.parse_qsl(url.query))
        p = url.path
        if method != "GET":
            self.writes.append((method, p))
        if (method, p) == ("POST", "/api/v1/auth/universal-auth/login"):
            ok = any(c == body["clientId"] and body["clientSecret"] in s
                     for c, s in ((self.ua[i]["clientId"], self.client_secrets.get(i, set())) for i in self.ua))
            if not ok:
                raise SystemExit("401")
            return {"accessToken": "t"}
        if (method, p) == ("GET", "/api/v1/projects"):
            return {"projects": self.projects}
        if (method, p) == ("POST", "/api/v1/projects"):
            self.projects.append({"id": f"proj-{body['slug']}", "slug": body["slug"]})
            return {"project": self.projects[-1]}
        if (method, p) == ("GET", "/api/v2/folders"):
            return {"folders": [{"name": n} for n in sorted(self.folders)]}
        if (method, p) == ("POST", "/api/v2/folders"):
            self.folders.add(body["name"])
            return {}
        if (method, p) == ("GET", "/api/v4/secrets"):
            return {"secrets": [{"secretKey": k} for (path_, k) in self.secrets if path_ == q["secretPath"]]}
        if m := re.fullmatch(r"/api/v4/secrets/(\w+)", p):
            self.secrets[(body["secretPath"], m.group(1))] = body["secretValue"]
            return {}
        if (method, p) == ("GET", "/api/v1/identities"):
            return {"identities": [{"identity": {"name": n, "id": i}} for n, i in self.identities.items()]}
        if (method, p) == ("POST", "/api/v1/identities"):
            self.identities[body["name"]] = f"id-{body['name']}"
            return {"identity": {"id": self.identities[body["name"]]}}
        if m := re.fullmatch(r"/api/v1/auth/universal-auth/identities/([\w-]+)(/client-secrets.*)?", p):
            iid, rest = m.group(1), m.group(2) or ""
            if rest == "" and method == "GET":
                if iid not in self.ua:
                    raise SystemExit("404")
                return {"identityUniversalAuth": self.ua[iid]}
            if rest == "" and method == "POST":
                self.ua[iid] = {"clientId": f"cid-{iid}", "accessTokenTTL": body["accessTokenTTL"]}
                return {"identityUniversalAuth": self.ua[iid]}
            if rest == "/client-secrets" and method == "GET":
                return {"clientSecretData": [{"id": s, "isClientSecretRevoked": False} for s in self.client_secrets.get(iid, ())]}
            if rest == "/client-secrets" and method == "POST":
                secret = f"secret-{iid}-{len(self.writes)}"
                self.client_secrets.setdefault(iid, set()).add(secret)
                return {"clientSecret": secret}
            if rest.endswith("/revoke"):
                self.client_secrets[iid].discard(rest.split("/")[2])
                return {}
        if m := re.fullmatch(r"/api/v1/projects/([\w-]+)/identity-memberships(?:/([\w-]+))?", p):
            if method == "GET":
                return {"identityMemberships": [{"identity": {"id": i}, "roles": [{"role": r}]}
                                                for (proj, i), r in self.members.items() if proj == m.group(1)]}
            self.members[(m.group(1), m.group(2))] = body["role"] if method == "POST" else body["roles"][0]["role"]
            return {}
        raise AssertionError(f"unerwarteter Aufruf {method} {path}")


class FakeKomodo:
    def __init__(self):
        self.variables = {}
        self.stack = {"server_id": "srv-1", "run_directory": RUN_DIR, "compose_cmd_wrapper": "",
                      "compose_cmd_wrapper_include": []}
        self.writes = []

    def read(self, typ, params=None):
        if typ == "ListVariables":
            return list(self.variables.values())
        if typ == "ListServers":
            return [{"id": "srv-1", "name": "worker-1"}]
        if typ == "GetStack":
            assert params == {"stack": "searxng"}
            return {"config": dict(self.stack)}
        raise AssertionError(typ)

    def write(self, typ, params):
        self.writes.append(typ)
        if typ == "CreateVariable":
            self.variables[params["name"]] = {k: params[k] for k in ("name", "value", "is_secret")}
        elif typ == "UpdateVariableValue":
            self.variables[params["name"]]["value"] = params["value"]
        elif typ == "UpdateStack":
            self.stack.update(params["config"])
        else:
            raise AssertionError(typ)


def run(inf, kom, apply=True, imports=None):
    return ss.sync(inf, kom, [STACK], "http://infisical:8079", imports or {}, apply)


def test_wrapper_uses_clone_of_server():
    w = ss.wrapper(STACK, RUN_DIR + "/")
    assert "INFISICAL_PROJECT_ID='[[INFISICAL_PROJECT_ID_WORKER_1]]'" in w
    assert w.endswith("sh /srv/periphery/data/komodo/repos/infrastructure-worker"
                      "/infra/infisical/compose-wrap.sh /searxng -- [[COMPOSE_COMMAND]]")
    assert "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET='[[INFISICAL_CLIENT_SECRET_WORKER_1]]'" in w
    with pytest.raises(SystemExit, match="does not end with"):
        ss.wrapper(STACK, "/srv/other/searxng")


def test_parse_env_file():
    text = "# Kommentar\nA=1\nexport B='x y'\nC=\"z # kein Kommentar\"\nD=w # Kommentar\nE=\n  F = g\n"
    assert ss.parse_env_file(text) == {"A": "1", "B": "x y", "C": "z # kein Kommentar", "D": "w", "F": "g"}


def test_dry_run_writes_nothing():
    inf, kom = FakeInfisical(), FakeKomodo()
    assert run(inf, kom, apply=False, imports={"searxng": {"SEARXNG_SECRET": "s"}}) == 0
    assert inf.writes == [] and kom.writes == []


def test_apply_sets_up_everything_and_is_idempotent():
    inf, kom = FakeInfisical(), FakeKomodo()
    run(inf, kom, imports={"searxng": {"SEARXNG_SECRET": "alt"}})
    iid, pid = inf.identities["stacks-worker-1"], "proj-stacks-worker-1"
    assert [p["slug"] for p in inf.projects] == ["stacks-worker-1"]
    assert inf.folders == {"searxng"} and inf.secrets == {("/searxng", "SEARXNG_SECRET"): "alt"}
    assert inf.ua[iid]["accessTokenTTL"] == ss.TOKEN_TTL and inf.members == {(pid, iid): "viewer"}
    v = kom.variables
    assert v["INFISICAL_PROJECT_ID_WORKER_1"]["value"] == pid and not v["INFISICAL_PROJECT_ID_WORKER_1"]["is_secret"]
    assert v["INFISICAL_CLIENT_ID_WORKER_1"]["value"] == inf.ua[iid]["clientId"]
    assert v["INFISICAL_CLIENT_SECRET_WORKER_1"]["is_secret"]
    assert v["INFISICAL_CLIENT_SECRET_WORKER_1"]["value"] in inf.client_secrets[iid]
    assert kom.stack["compose_cmd_wrapper"] == ss.wrapper(STACK, RUN_DIR)
    assert kom.stack["compose_cmd_wrapper_include"] == ss.WRAP_INCLUDE

    inf.writes.clear()
    kom.writes = []
    run(inf, kom, imports={"searxng": {"SEARXNG_SECRET": "neu"}})
    assert inf.secrets[("/searxng", "SEARXNG_SECRET")] == "alt"  # nie überschreiben
    assert kom.writes == []
    assert [w for w in inf.writes if w[1] != "/api/v1/auth/universal-auth/login"] == []


def test_wrong_project_role_is_corrected():
    inf, kom = FakeInfisical(), FakeKomodo()
    run(inf, kom)
    key = next(iter(inf.members))
    inf.members[key] = "no-access"
    run(inf, kom)
    assert inf.members[key] == "viewer"


def test_invalid_secret_in_komodo_is_replaced_and_old_revoked():
    inf, kom = FakeInfisical(), FakeKomodo()
    run(inf, kom)
    iid = inf.identities["stacks-worker-1"]
    kom.variables["INFISICAL_CLIENT_SECRET_WORKER_1"]["value"] = "kaputt"
    run(inf, kom)
    assert len(inf.client_secrets[iid]) == 1
    assert kom.variables["INFISICAL_CLIENT_SECRET_WORKER_1"]["value"] in inf.client_secrets[iid]


def test_stack_on_other_server_is_refused():
    inf, kom = FakeInfisical(), FakeKomodo()
    kom.stack["server_id"] = "srv-x"
    with pytest.raises(SystemExit, match="inventory says worker-1"):
        run(inf, kom)


def test_import_requires_secret_stack():
    with pytest.raises(SystemExit, match="without `secrets: infisical`"):
        run(FakeInfisical(), FakeKomodo(), imports={"immich": {"A": "1"}})


def test_a_project_slug_is_lower_case_letters_digits_and_hyphens():
    assert ss.project_slug("box-c") == "stacks-box-c"          # the names of existing installations do not change
    assert ss.project_slug("Local") == "stacks-local"                      # Komodo's default server
    assert ss.project_slug("My_Box.1") == "stacks-my-box-1"

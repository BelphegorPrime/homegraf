"""Fake HTTP services for the tests: a real server on a free local port, driven by a handler function."""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Req:
    def __init__(self, method, path, headers, body):
        self.method, self.path, self.headers, self.body = method, path, headers, body

    @property
    def json(self):
        return json.loads(self.body) if self.body else None

    def basic(self):
        auth = self.headers.get("authorization", "")
        if auth.startswith("Basic "):
            user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            return user, password
        return None


class Server:
    """handler(req) -> (status, payload); payload is JSON-encoded, None means an empty body. Every request is kept in .requests."""

    def __init__(self, handler):
        self.requests: list[Req] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("content-length") or 0)
                req = Req(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(length))
                outer.requests.append(req)
                status, payload = handler(req)
                body = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def calls(self, method=None, prefix=""):
        return [r for r in self.requests if (method is None or r.method == method) and r.path.startswith(prefix)]


# -- Forgejo ----------------------------------------------------------------------------------------------------------------
class FakeForgejo:
    def __init__(self, admin="owner", password="pw-correct-1234"):
        self.admin, self.password = admin, password
        self.tokens: dict[str, str] = {}
        self.repos: dict[str, dict] = {}
        self.hooks: list[dict] = []
        self.commits: list[list[str]] = []
        self.users: dict[str, str] = {}              # other users, created through the admin API: name -> password
        self.user_tokens: dict[str, dict] = {}
        self.n = 0

    def authed(self, req):
        if req.basic() == (self.admin, self.password):
            return True
        if req.basic() and self.users.get(req.basic()[0]) == req.basic()[1]:
            return True
        auth = req.headers.get("authorization", "")
        return auth.startswith("token ") and (auth[6:] in self.tokens.values() or any(auth[6:] in u.values() for u in self.user_tokens.values()))

    def __call__(self, req):
        p = req.path.split("?")[0]
        if p == "/api/healthz":
            return 200, {"status": "pass"}
        if not self.authed(req):
            return 401, {"message": "unauthorized"}
        base = f"/api/v1/users/{self.admin}/tokens"
        bot = p.split("/")[4] if p.startswith("/api/v1/users/") and len(p.split("/")) > 4 else None
        if bot in self.users and p == f"/api/v1/users/{bot}/tokens":
            tokens = self.user_tokens.setdefault(bot, {})
            if req.method == "GET":
                return 200, [{"name": n} for n in tokens]
            self.n += 1
            tokens[req.json["name"]] = f"bot-tok-{self.n}"
            return 201, {"name": req.json["name"], "sha1": f"bot-tok-{self.n}", "scopes": req.json["scopes"]}
        if bot in self.users and p.startswith(f"/api/v1/users/{bot}/tokens/") and req.method == "DELETE":
            self.user_tokens.get(bot, {}).pop(p.rsplit("/", 1)[1], None)
            return 204, None
        if p.startswith("/api/v1/users/") and len(p.split("/")) == 5 and req.method == "GET":
            return (200, {"login": p.split("/")[4]}) if p.split("/")[4] in self.users or p.split("/")[4] == self.admin else (404, {})
        if p == "/api/v1/admin/users" and req.method == "POST":
            self.users[req.json["username"]] = req.json["password"]
            return 201, {"login": req.json["username"]}
        if p.startswith("/api/v1/admin/users/") and p.endswith("/repos") and req.method == "POST":
            self.repos[f"{p.split('/')[5]}/{req.json['name']}"] = {"empty": True, "files": {}, "private": req.json["private"], "secrets": {}, "collaborators": {}}
            return 201, {"full_name": f"{p.split('/')[5]}/{req.json['name']}"}
        if p == "/api/v1/user":
            return 200, {"login": self.admin}
        if p == base and req.method == "GET":
            return 200, [{"name": n} for n in self.tokens]
        if p == base and req.method == "POST":
            if req.json["name"] in self.tokens:
                return 400, {"message": "access token name has been used already"}
            self.n += 1
            self.tokens[req.json["name"]] = f"tok-{self.n}"
            return 201, {"name": req.json["name"], "sha1": f"tok-{self.n}", "scopes": req.json["scopes"]}
        if p.startswith(base + "/") and req.method == "DELETE":
            self.tokens.pop(p.rsplit("/", 1)[1], None)
            return 204, None
        if p == "/api/v1/user/repos" and req.method == "POST":
            self.repos[f"{self.admin}/{req.json['name']}"] = {"empty": True, "files": {}, "private": req.json["private"]}
            return 201, {"full_name": f"{self.admin}/{req.json['name']}"}
        parts = p.split("/")                     # ['', 'api', 'v1', 'repos', owner, name, ...]
        if len(parts) >= 6 and parts[3] == "repos":
            name = f"{parts[4]}/{parts[5]}"
            if name not in self.repos:
                return 404, {"message": "not found"}
            repo, rest = self.repos[name], parts[6:]
            if not rest:
                return 200, {"full_name": name, "empty": repo["empty"]}
            if rest == ["contents"] and req.method == "POST":
                self.commits.append([f["path"] for f in req.json["files"]])
                repo.setdefault("events", []).append("commit")
                repo["empty"] = False
                repo["files"].update({f["path"]: f["content"] for f in req.json["files"]})
                return 201, {"commit": {"sha": "abc"}}
            if rest[:1] == ["contents"] and req.method == "GET":
                path = "/".join(rest[1:])
                if repo["empty"]:
                    return 200, []                       # what Forgejo answers for any path of an empty repository
                if path not in repo["files"]:
                    return 404, {"message": "not found"}
                return 200, {"content": repo["files"][path], "sha": f"sha-{len(repo['files'][path])}"}
            if rest[:2] == ["actions", "secrets"] and req.method == "PUT":
                repo.setdefault("secrets", {})[rest[2]] = req.json["data"]
                repo.setdefault("events", []).append("secret")
                return 204, None
            if rest[:1] == ["collaborators"] and req.method == "PUT":
                repo.setdefault("collaborators", {})[rest[1]] = req.json["permission"]
                return 204, None
            if rest == ["hooks"] and req.method == "GET":
                return 200, self.hooks
            if rest == ["hooks"] and req.method == "POST":
                self.hooks.append(req.json)
                return 201, {"id": len(self.hooks)}
        return 404, {"message": f"unexpected {req.method} {p}"}


# -- Komodo -----------------------------------------------------------------------------------------------------------------
class FakeKomodo:
    def __init__(self, admin="admin", password="pw-correct-1234", server_state="Ok", clone_ok=True):
        self.admin, self.password, self.server_state, self.clone_ok = admin, password, server_state, clone_ok
        self.keys: list[dict] = []
        self.accounts: list[dict] = []
        self.repos: dict[str, dict] = {}
        self.procedures: dict[str, dict] = {"Backup Core Database": {}}
        self.updates: dict[str, dict] = {}
        self.stacks: dict[str, dict] = {}
        self.ingress_calls: list[list[str]] = []
        self.ingress_ok = True
        self.n = 0

    def ingressctl(self, command):
        return (True, '{"result": "ok"}') if self.ingress_ok else (True, '{"result": "error"}')

    def new_id(self):
        self.n += 1
        return f"{self.n:024x}"

    def __call__(self, req):
        p, body = req.path, req.json
        if p == "/version":
            return 200, {"version": "2.3.4"}
        if p == "/auth/login/LoginLocalUser":
            if (body["username"], body["password"]) == (self.admin, self.password):
                return 200, {"type": "Jwt", "data": {"jwt": "jwt-1"}}
            return 401, {"error": "invalid"}
        by_key = req.headers.get("x-api-key")
        if not (req.headers.get("authorization") == "jwt-1" or any(k["key"] == by_key and k["secret"] == req.headers.get("x-api-secret") for k in self.keys)):
            return 401, {"error": "unauthorized"}
        if p == "/auth/manage/CreateApiKey":
            key = {"key": f"K_{self.new_id()}", "secret": f"S_{self.new_id()}", "name": body["name"]}
            self.keys.append(key)
            return 200, {"key": key["key"], "secret": key["secret"]}
        if p == "/auth/manage/DeleteApiKey":
            self.keys = [k for k in self.keys if k["key"] != body["key"]]
            return 200, {}
        if p == "/read/GetVersion":
            return 200, {"version": "2.3.4"}
        if p == "/read/ListApiKeys":
            return 200, [{"key": k["key"], "secret": "", "name": k["name"]} for k in self.keys]
        if p == "/read/ListServers":
            return 200, [{"id": "s1", "name": "Local", "info": {"state": self.server_state}}]
        if p == "/read/ListGitProviderAccounts":
            return 200, self.accounts
        if p == "/write/CreateGitProviderAccount":
            self.accounts.append({"_id": {"$oid": self.new_id()}, **body["account"]})
            return 200, {}
        if p == "/write/UpdateGitProviderAccount":
            next(a for a in self.accounts if a["_id"]["$oid"] == body["id"]).update(body["account"])
            return 200, {}
        if p == "/read/ListRepos":
            return 200, [{"id": r["id"], "name": n} for n, r in self.repos.items()]
        if p == "/read/GetRepo":
            return 200, self.repos[body["repo"]]
        if p == "/write/CreateRepo":
            self.repos[body["name"]] = {"id": self.new_id(), "config": dict(body["config"]), "info": {"latest_hash": None}}
            return 200, {}
        if p == "/write/UpdateRepo":
            next(r for r in self.repos.values() if r["id"] == body["id"])["config"].update(body["config"])
            return 200, {}
        if p == "/execute/CloneRepo":
            uid = self.new_id()
            ok = self.clone_ok
            if ok:
                self.repos[body["repo"]]["info"]["latest_hash"] = "25de55c"
            self.updates[uid] = {"status": "Complete", "success": ok, "logs": [{"stage": "Clone Repo", "success": ok, "stderr": "" if ok else "fatal: could not read Username"}]}
            return 200, {"_id": {"$oid": uid}}
        if p == "/read/GetUpdate":
            return 200, self.updates[body["id"]]
        if p == "/read/ListProcedures":
            return 200, [{"name": n} for n in self.procedures]
        if p == "/write/CreateProcedure":
            self.procedures[body["name"]] = body["config"]
            return 200, {}
        if p == "/read/GetProcedure":
            return 200, {"_id": {"$oid": "proc-" + body["procedure"]}, "config": self.procedures[body["procedure"]]}
        if p == "/write/UpdateProcedure":
            self.procedures[body["id"].removeprefix("proc-")].update(body["config"])
            return 200, {}
        if p == "/read/ListStacks":
            return 200, [{"name": n} for n in self.stacks]
        if p == "/write/CreateStack":
            self.stacks[body["name"]] = body["config"]
            return 200, {}
        if p == "/write/RefreshStackCache":
            return 200, {}
        if p in ("/execute/PullRepo", "/execute/DeployStack", "/execute/RunStackService"):
            uid = self.new_id()
            out = ""
            if p == "/execute/RunStackService":
                self.ingress_calls.append(body["command"])
                ok, out = self.ingressctl(body["command"])
            else:
                ok = True
            self.updates[uid] = {"status": "Complete", "success": True, "logs": [{"stage": "run", "success": True, "stdout": out}]}
            return 200, {"_id": {"$oid": uid}}
        return 404, {"error": f"unexpected {p}"}


# -- Nginx Proxy Manager ----------------------------------------------------------------------------------------------------
class FakeNpm:
    def __init__(self, set_up=False, email="admin@example.org", password="pw-correct-1234"):
        self.users = {email: password} if set_up else {}
        self.user_ids: dict[str, int] = {}
        self.permissions: dict[int, dict] = {}
        self.hosts: list[dict] = []
        self.unknown_hosts_for = 0         # how many proxy requests answer 404 before the host is "reloaded"

    def __call__(self, req):
        p = req.path
        if p == "/api/":
            return 200, {"status": "OK", "setup": bool(self.users)}
        if p == "/api/users" and req.method == "POST" and not req.headers.get("authorization"):
            if self.users:
                return 400, {"error": "already set up"}
            self.users[req.json["email"]] = req.json["auth"]["secret"]
            self.user_ids[req.json["email"]] = len(self.user_ids) + 1
            return 201, {"id": self.user_ids[req.json["email"]]}
        if p == "/api/tokens":
            if self.users.get(req.json["identity"]) == req.json["secret"]:
                return 200, {"token": "npm-token", "expires": "x"}
            return 400, {"error": {"message": "Invalid email or password"}}
        if req.headers.get("authorization") != "Bearer npm-token":
            return 401, {}
        if p == "/api/users" and req.method == "GET":
            return 200, [{"id": i, "email": e} for e, i in self.user_ids.items()]
        if p == "/api/users" and req.method == "POST":
            self.users[req.json["email"]] = req.json["auth"]["secret"]
            self.user_ids[req.json["email"]] = len(self.user_ids) + 1
            return 201, {"id": self.user_ids[req.json["email"]], "email": req.json["email"]}
        if p.startswith("/api/users/") and p.endswith("/permissions") and req.method == "PUT":
            self.permissions[int(p.split("/")[3])] = req.json
            return 200, True
        if p.startswith("/api/users/") and p.endswith("/auth") and req.method == "PUT":
            email = next(e for e, i in self.user_ids.items() if i == int(p.split("/")[3]))
            self.users[email] = req.json["secret"]
            return 200, True
        if p == "/api/nginx/proxy-hosts" and req.method == "GET":
            return 200, self.hosts
        if p == "/api/nginx/proxy-hosts" and req.method == "POST":
            self.hosts.append({"id": len(self.hosts) + 1, **req.json})
            return 201, self.hosts[-1]
        if p.startswith("/api/nginx/proxy-hosts/") and req.method == "PUT":
            i = int(p.rsplit("/", 1)[1])
            self.hosts = [{"id": i, **req.json} if h["id"] == i else h for h in self.hosts]
            return 200, {}
        return 404, {"error": p}

    def proxy(self, req):                   # the proxy's port 80 (a different Server)
        if self.unknown_hosts_for > 0:
            self.unknown_hosts_for -= 1
            return 404, None
        return 502, None


# -- Infisical --------------------------------------------------------------------------------------------------------------
def jwt(**claims) -> str:
    import base64, json
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class FakeInfisical:
    """Instance bootstrap, identities with Universal Auth, and the Certificate Manager (projects, CA, policy, profile, application)."""
    def __init__(self, initialized=False):
        self.initialized, self.identities, self.secrets, self.admin_password = initialized, {}, {}, None
        self.projects, self.cas, self.policies, self.profiles, self.apps = [], [], [], [], []
        self.project_members, self.app_members, self.api_enrolled = [], [], set()
        self.active_project = None
        self.token = jwt(orgId="org1")
        self.n = 0

    def uid(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n}"

    def __call__(self, req):
        p, body = req.path.split("?")[0], req.json
        if p == "/api/status":
            return 200, {"message": "Ok"}
        if p == "/api/v1/admin/config":
            return 200, {"config": {"initialized": self.initialized}}
        if p == "/api/v1/admin/bootstrap":
            if self.initialized:
                return 400, {"message": "already"}
            self.initialized, self.admin_password = True, body["password"]
            return 200, {"identity": {"credentials": {"token": "admin-token"}}, "organization": {"id": "org1"}, "user": {}}
        if p == "/api/v1/auth/universal-auth/login":
            ok = self.secrets.get(body["clientId"]) == body["clientSecret"]
            return (200, {"accessToken": self.token}) if ok else (401, {"message": "bad"})
        if req.headers.get("authorization") not in ("Bearer admin-token", f"Bearer {self.token}"):
            return 401, {}
        return self.api(req, p, body)

    def api(self, req, p, body):
        m = req.method
        if p == "/api/v1/identities":
            iid = f"id{len(self.identities) + 1}"
            self.identities[iid] = body
            return 200, {"identity": {"id": iid}}
        if p.startswith("/api/v1/auth/universal-auth/identities/"):
            iid, _, rest = p.rsplit("identities/", 1)[1].partition("/")
            cid = f"client-{iid[2:]}"
            if rest == "" and m == "GET":
                return (200, {"identityUniversalAuth": {"clientId": cid}}) if f"ua-{iid}" in self.secrets else (404, {})
            if rest == "":
                self.secrets[f"ua-{iid}"] = ""
                return 200, {"identityUniversalAuth": {"clientId": cid}}
            if rest == "client-secrets" and m == "GET":
                return 200, {"clientSecretData": []}
            if rest == "client-secrets":
                self.secrets[cid] = f"secret-{iid[2:]}"
                return 200, {"clientSecret": self.secrets[cid]}
        if p == "/api/v1/projects" and m == "GET":
            return 200, {"projects": self.projects}
        if p == "/api/v1/projects" and m == "POST":
            self.projects.append({"id": self.uid("proj"), "slug": body["slug"], "type": body["type"]})
            return 200, {"project": self.projects[-1]}
        if p.startswith("/api/v1/projects/") and "/identity-memberships" in p:
            if m == "GET":
                return 200, {"identityMemberships": [{"identity": {"id": i}} for i in self.project_members]}
            self.project_members.append(p.rsplit("/", 1)[1])
            return 200, {}
        if p == "/api/v1/cert-manager/instance/active-project":
            self.active_project = body["projectId"]
            return 200, {"activeProjectId": self.active_project}
        if p == "/api/v1/cert-manager/ca" and m == "GET":
            return 200, {"certificateAuthorities": self.cas}
        if p == "/api/v1/cert-manager/ca/internal" and m == "POST":
            self.cas.append({"id": self.uid("ca"), "projectId": body["projectId"], "name": body["name"], "type": "internal", "status": "active", "configuration": body["configuration"]})
            return 200, self.cas[-1]
        if p.startswith("/api/v1/cert-manager/ca/internal/") and p.endswith("/certificate"):
            return 200, {"certificate": "-----BEGIN CERTIFICATE-----\nROOT\n-----END CERTIFICATE-----"}
        if p == "/api/v1/cert-manager/certificate-policies" and m == "GET":
            return 200, {"certificatePolicies": self.policies}
        if p == "/api/v1/cert-manager/certificate-policies" and m == "POST":
            self.policies.append({"id": self.uid("pol"), **body})
            return 200, {"certificatePolicy": self.policies[-1]}
        if p == "/api/v1/cert-manager/certificate-profiles" and m == "GET":
            return 200, {"certificateProfiles": self.profiles}
        if p == "/api/v1/cert-manager/certificate-profiles" and m == "POST":
            self.profiles.append({"id": self.uid("prof"), **body})
            return 200, {"certificateProfile": self.profiles[-1]}
        if p == "/api/v1/cert-manager/applications" and m == "GET":
            return 200, {"applications": self.apps}
        if p == "/api/v1/cert-manager/applications" and m == "POST":
            self.apps.append({"id": self.uid("app"), "name": body["name"], "profiles": list(body["profileIds"])})
            return 200, {"application": self.apps[-1]}
        if p.startswith("/api/v1/cert-manager/applications/"):
            app_id, _, rest = p.rsplit("applications/", 1)[1].partition("/")
            app = next(a for a in self.apps if a["id"] == app_id)
            if rest == "profiles" and m == "GET":
                return 200, {"profiles": [{"profileId": x, "apiConfigId": "api" if (app_id, x) in self.api_enrolled else None} for x in app["profiles"]]}
            if rest == "profiles":
                app["profiles"] += [x for x in body["profileIds"] if x not in app["profiles"]]
                return 200, {}
            if rest.endswith("/enrollment/api"):
                self.api_enrolled.add((app_id, rest.split("/")[1]))
                return 200, {}
            if rest == "identities" and m == "GET":
                return 200, {"memberships": [{"actorIdentityId": i} for i in self.app_members]}
            if rest.startswith("identities/"):
                self.app_members.append(rest.split("/")[1])
                return 200, {}
        return 404, {"unexpected": f"{m} {p}"}


# -- AdGuard Home -----------------------------------------------------------------------------------------------------------
class FakeAdGuard:
    """Both ports in one: before the setup the install endpoints answer, afterwards the control API (basic auth)."""

    def __init__(self, user="admin", password="pw-correct-1234", configured=False):
        self.user, self.password, self.configured = user, password, configured
        self.rewrites: list[dict] = []

    def __call__(self, req):
        p = req.path
        if p == "/control/install/get_addresses":
            return (404, None) if self.configured else (200, {"web_port": 3000})
        if p == "/control/install/configure":
            self.configured = True
            return 200, {}
        if not self.configured or req.basic() != (self.user, self.password):
            return 403, None
        if p == "/control/status":
            return 200, {"running": True}
        if p == "/control/rewrite/list":
            return 200, self.rewrites
        if p == "/control/rewrite/add":
            self.rewrites.append(req.json)
            return 200, None
        if p == "/control/rewrite/delete":
            self.rewrites = [r for r in self.rewrites if r != req.json]
            return 200, None
        return 404, None

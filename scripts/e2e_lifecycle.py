"""The part of scripts/e2e-stack.sh that talks to a running stack: HG_URL (homegraf), FORGEJO_URL, HG_PASSWORD (admin and owner)."""
import base64
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

HG, FORGEJO, PASSWORD = os.environ["HG_URL"], os.environ["FORGEJO_URL"], os.environ["HG_PASSWORD"]


def basic(user):
    return "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()


def call(base, user, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"authorization": basic(user), "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


def check(ok, what):
    print(("   ok   " if ok else "   FAIL ") + what, flush=True)
    if not ok:
        sys.exit(1)


def job_result(job):
    for _ in range(150):
        time.sleep(4)
        _, j = call(HG, "admin", "GET", f"/api/apps/jobs/{job}")
        if j.get("state") != "running":
            return j
    check(False, f"job {job} did not finish")


def answers(url="http://127.0.0.1:8000/"):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def wait(fn, what, tries=40, pause=3):
    for _ in range(tries):
        if fn():
            return check(True, what)
        time.sleep(pause)
    check(False, what)


def merge(number):
    st, _ = call(FORGEJO, "owner", "POST", f"/api/v1/repos/owner/infrastructure/pulls/{number}/merge", {"Do": "merge"})
    check(st == 200, f"pull request #{number} merged")


st, caps = call(HG, "admin", "GET", "/api/capabilities")
check(caps["create"]["enabled"] and caps["users"]["enabled"], "homegraf has the write actions switched on")
st, g = call(HG, "admin", "GET", "/api/graph")
check(st == 200 and not g["warnings"] and any(n["type"] == "infra" for n in g["nodes"]), "the map is built from the seed, without warnings")
server = next(n["label"] for n in g["nodes"] if n["type"] == "machine")

st, d = call(HG, "admin", "POST", "/api/apps/create", {"appId": "probe", "server": server, "image": "traefik/whoami:v1.11.0", "containerPort": 80,
                                                      "hostPort": 8000, "routing": False, "secretVars": ["API_TOKEN"]})
check(st == 202, "create accepted")
j = job_result(d["jobId"])
check(j["state"] == "done" and j.get("pullRequestUrl"), f"create finished with a pull request ({j.get('pullRequestUrl')})")
merge(1)
wait(answers, "the merge deployed the app (webhook, update-all, deploy) and it answers")

st, d = call(HG, "admin", "POST", "/api/apps/remove", {"appId": "probe", "confirm": "probe"})
check(st == 202, "remove accepted")
j = job_result(d["jobId"])
check(j["state"] == "done" and j.get("pullRequestUrl"), "first half done: stack destroyed, pull request open")
check(not answers(), "the app is down")
_, p = call(HG, "admin", "GET", "/api/removals")
check([r["appId"] for r in p["removals"]] == ["probe"], "the removal waits for its merge")
merge(2)
wait(lambda: call(HG, "admin", "GET", "/api/removals")[1]["removals"][0]["pr"] == "merged", "homegraf sees the merge")
st, d = call(HG, "admin", "POST", "/api/apps/remove/finish", {"appId": "probe"})
check(st == 202, "finish accepted")
j = job_result(d["jobId"])
check(j["state"] == "done", "second half done (secrets removed)")
check(call(HG, "admin", "GET", "/api/removals")[1]["removals"] == [], "nothing waits any more")


# An app with an address: after its merge the proxy serves it over https with a certificate of the stack's CA (update-all ends with
# ingress-apply), and after the removal it does not any more. Needs DOMAIN, ROOT_CA and HTTPS_PORT (the proxy's 443 on this host).
DOMAIN, ROOT_CA, HTTPS_PORT = os.environ.get("DOMAIN"), os.environ.get("ROOT_CA"), int(os.environ.get("HTTPS_PORT", "0"))
if DOMAIN and ROOT_CA and HTTPS_PORT:
    host = f"web.{DOMAIN}"

    def https_answers():
        """True when the proxy serves `host` with a certificate that verifies against the CA and the app answers behind it."""
        try:
            ctx = ssl.create_default_context(cafile=ROOT_CA)
            with socket.create_connection(("127.0.0.1", HTTPS_PORT), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname=host) as s:
                conn = http.client.HTTPSConnection(host, HTTPS_PORT, timeout=5)
                conn.sock = s
                conn.request("GET", "/")
                return conn.getresponse().status == 200
        except (OSError, ssl.SSLError, http.client.HTTPException):
            return False

    def number(job):
        return int(job["pullRequestUrl"].rstrip("/").rsplit("/", 1)[1])

    st, d = call(HG, "admin", "POST", "/api/apps/create", {"appId": "web", "server": server, "image": "traefik/whoami:v1.11.0", "containerPort": 80,
                                                          "hostPort": 8001, "routing": True})
    check(st == 202, "create with an address accepted")
    j = job_result(d["jobId"])
    check(j["state"] == "done" and j.get("pullRequestUrl"), f"created with a pull request ({j.get('pullRequestUrl')})")
    merge(number(j))
    wait(https_answers, f"https://{host} answers with a certificate of the CA (update-all ended with ingress-apply)", tries=100)

    st, d = call(HG, "admin", "POST", "/api/apps/remove", {"appId": "web", "confirm": "web"})
    check(st == 202, f"remove accepted ({st} {d})")
    j = job_result(d["jobId"])
    check(j["state"] == "done" and j.get("pullRequestUrl"), "first half done")
    merge(number(j))
    wait(lambda: call(HG, "admin", "GET", "/api/removals")[1]["removals"][0]["pr"] == "merged", "homegraf sees the merge")
    st, d = call(HG, "admin", "POST", "/api/apps/remove/finish", {"appId": "web"})
    check(st == 202, "finish accepted")
    j = job_result(d["jobId"])
    check(j["state"] == "done", "second half done")
    check(not https_answers(), f"https://{host} is gone: the proxy host and its certificate were removed")

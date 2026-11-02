"""The part of scripts/e2e-host.sh that talks to the stack after the second host was added: HG_URL, FORGEJO_URL, HG_PASSWORD, HG_USER,
HOST_NAME (the new host), HOST_ADDRESS (where its apps answer) and HOST_DOCKER (the container whose Docker is that host)."""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HG, FORGEJO, PASSWORD, ADMIN = os.environ["HG_URL"], os.environ["FORGEJO_URL"], os.environ["HG_PASSWORD"], os.environ["HG_USER"]
NAME, ADDRESS, HOST_DOCKER = os.environ["HOST_NAME"], os.environ["HOST_ADDRESS"], os.environ["HOST_DOCKER"]


def call(base, user, method, path, body=None):
    auth = "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"authorization": auth, "content-type": "application/json"})
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


def wait(fn, what, tries=60, pause=4):
    for _ in range(tries):
        if fn():
            return check(True, what)
        time.sleep(pause)
    check(False, what)


def job_result(job):
    for _ in range(150):
        time.sleep(4)
        _, j = call(HG, ADMIN, "GET", f"/api/apps/jobs/{job}")
        if j.get("state") != "running":
            return j
    check(False, f"job {job} did not finish")


def answers(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def containers():
    return subprocess.run(["docker", "exec", HOST_DOCKER, "docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()


def merge(number):
    st, _ = call(FORGEJO, "owner", "POST", f"/api/v1/repos/owner/infrastructure/pulls/{number}/merge", {"Do": "merge"})
    check(st == 200, f"pull request #{number} merged")


def machines():
    st, g = call(HG, ADMIN, "GET", "/api/graph")
    return [n["label"] for n in g["nodes"] if n["type"] == "machine"] if st == 200 else []


# the push of servers.yaml ran update-all, which pulled the clone of the first host; the map reads that clone
wait(lambda: NAME in machines(), f"the map shows the host {NAME} (inventory/servers.yaml reached the repository and its clone)")

st, d = call(HG, ADMIN, "POST", "/api/apps/create", {"appId": "probeb", "server": NAME, "image": "traefik/whoami:v1.11.0", "containerPort": 80,
                                                      "hostPort": 8000, "routing": False})
check(st == 202, f"create on {NAME} accepted")
j = job_result(d["jobId"])
check(j["state"] == "done" and j.get("pullRequestUrl"), f"created with a pull request ({j.get('pullRequestUrl')})")
merge(1)
wait(lambda: answers(f"http://{ADDRESS}:8000/"), f"the merge deployed the app on {NAME} (webhook, update-all pulls both clones, deploy) and it answers")
check(any(c.startswith("probeb") for c in containers()), f"the container runs in the Docker of {NAME}")

st, d = call(HG, ADMIN, "POST", "/api/apps/remove", {"appId": "probeb", "confirm": "probeb"})
check(st == 202, "remove accepted")
j = job_result(d["jobId"])
check(j["state"] == "done" and j.get("pullRequestUrl"), "first half done: the stack on the host is destroyed")
check(not answers(f"http://{ADDRESS}:8000/") and not any(c.startswith("probeb") for c in containers()), "the app is gone from the host")
merge(2)
wait(lambda: call(HG, ADMIN, "GET", "/api/removals")[1]["removals"][0]["pr"] == "merged", "homegraf sees the merge")
st, d = call(HG, ADMIN, "POST", "/api/apps/remove/finish", {"appId": "probeb"})
check(st == 202 and job_result(d["jobId"])["state"] == "done", "second half done")

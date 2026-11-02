"""The sign-in through the stack's own Forgejo, played like a browser (run in the stack's network, where git.<domain> and
homegraf.<domain> answer on port 80, by DOMAIN, FORGEJO_USER, ADMIN_EMAIL, ADMIN_PASSWORD): homegraf sends to Forgejo, the
administrator signs in there with the e-mail address, Forgejo sends back, homegraf makes the administrator its first administrator."""
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DOMAIN, OWNER, EMAIL, PASSWORD = (os.environ[k] for k in ("DOMAIN", "FORGEJO_USER", "ADMIN_EMAIL", "ADMIN_PASSWORD"))
GIT, HG = f"http://git.{DOMAIN}", f"http://homegraf.{DOMAIN}"
jar = http.cookiejar.CookieJar()


class Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), Stay)


def fetch(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    try:
        r = opener.open(urllib.request.Request(url, data=body), timeout=30)
    except urllib.error.HTTPError as e:
        r = e
    return r.status, r.headers.get("location"), r.read().decode("utf-8", "replace")


def check(ok, what, detail=""):
    print(("   ok   " if ok else "   FAIL ") + what + ("" if ok else f" ({detail})"))
    if not ok:
        sys.exit(1)


status, location, _ = fetch(f"{HG}/api/auth/login")
check(status == 302 and location and location.startswith(f"{GIT}/login/oauth/authorize?"), "homegraf sends the browser to its own Forgejo", f"{status} {location}")
authorize = location

_, _, page = fetch(f"{GIT}/user/login")
check('name="user_name"' in page, "Forgejo's sign-in page")
csrf = re.search(r'name="_csrf" value="([^"]+)"', page)     # older Forgejo; newer ones judge by the request's headers instead
status, _, _ = fetch(f"{GIT}/user/login", {**({"_csrf": csrf.group(1)} if csrf else {}), "user_name": EMAIL, "password": PASSWORD})
check(status in (302, 303), "the administrator signs in at Forgejo with the e-mail address", status)

status, location, page = fetch(authorize)
if status == 200:     # Forgejo asks whether to allow the application
    form = dict(re.findall(r'name="([a-z_]+)"\s+value="([^"]*)"', page))
    form = {k: v.replace("&amp;", "&") for k, v in form.items() if k in ("_csrf", "client_id", "state", "scope", "nonce", "redirect_uri", "code_challenge", "code_challenge_method", "response_type")}
    status, location, page = fetch(f"{GIT}/login/oauth/grant", {**form, "granted": "true"})
    print("        (Forgejo asked for the allowance and got it)")
check(status in (302, 303) and location and location.startswith(f"{HG}/api/auth/callback?"), "Forgejo sends the browser back with a code", f"{status} {location} {page[:200]}")

status, location, _ = fetch(location)
check(status == 302 and not (location or "").startswith("/?sso_error"), "homegraf accepts the code", f"{status} {location}")
check(any(c.name == "hg_session" for c in jar), "homegraf set the session")

status, _, body = fetch(f"{HG}/api/users")
users = json.loads(body).get("users", []) if status == 200 else []
check(status == 200, "the session may manage users (the first administrator has every permission)", status)
mine = next((u for u in users if u["name"] == OWNER), None)
check(mine is not None and mine.get("sso") is True and len(mine["permissions"]) == 6, f"{OWNER} is an entry of the Users tab", users)

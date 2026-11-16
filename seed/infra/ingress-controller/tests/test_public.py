import shutil
import subprocess

import pytest

from ingressctl.errors import ManifestError
from ingressctl.public import PublicSpec, parse_public, render_public_config

LONG_OK = "/" + "a" * 199
LONG_BAD = "/" + "a" * 200


def test_defaults_are_head_and_get_without_source_restriction():
    spec = parse_public({"paths": ["/a"]})
    assert spec == PublicSpec(paths=("/a",), methods=("GET", "HEAD"), allow_cidrs=())


def test_paths_are_sorted_and_unique():
    spec = parse_public({"paths": ["/z", "/a", "/z", "/a/b"]})
    assert spec.paths == ("/a", "/a/b", "/z")


@pytest.mark.parametrize("path", [
    "/a",
    "/a/b",
    "/a-b_c.d~e",
    "/a..b",
    "/a/*",
    "/a/b/*",
    "/.well-known/other",
    "/a/",
    LONG_OK,
])
def test_accepts_path(path):
    (parsed,) = parse_public({"paths": [path]}).paths
    assert parsed == path


@pytest.mark.parametrize("path", [
    "/",
    "/*",
    "/**",
    "",
    "a",
    "/a//b",
    "//a",
    "/a/../b",
    "/a/./b",
    "/..",
    "/.",
    "/a b",
    "/a;b",
    "/a{b",
    "/a}b",
    '/a"b',
    "/a'b",
    "/a\\b",
    "/a$b",
    "/a#b",
    "/a%b",
    "/a?b",
    "/a<b",
    "/a>b",
    "/a|b",
    "/a(b",
    "/a*b",
    "/a/*/b",
    "/a*",
    "/*/a",
    "/a\tb",
    "/a\nb",
    "/a\rb",
    "/a\u00e4",
    None,
    1,
    LONG_BAD,
])
def test_rejects_path(path):
    with pytest.raises(ManifestError, match="paths"):
        parse_public({"paths": [path]})


@pytest.mark.parametrize("raw,match", [
    ({}, "paths"),
    ({"paths": "/a"}, "paths"),
    ({"paths": []}, "paths"),
    ({"paths": ["/a"], "typo": 1}, "typo"),
    ({"paths": ["/a"], "preset": "github-hooks"}, "preset"),
    ({"paths": ["/a"], "allow_cidrs": {"preset": "github-hooks"}}, "preset"),
])
def test_rejects_bad_structure(raw, match):
    with pytest.raises(ManifestError, match=match):
        parse_public(raw)


@pytest.mark.parametrize("value", [None, [], "x"])
def test_raw_must_be_an_object(value):
    with pytest.raises(ManifestError, match="Objekt"):
        parse_public(value)


@pytest.mark.parametrize("raw,expected", [
    (["post"], ("POST",)),
    (["Head", "get", "POST", "get"], ("GET", "HEAD", "POST")),
    (["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
     ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")),
])
def test_accepts_methods(raw, expected):
    assert parse_public({"paths": ["/a"], "methods": raw}).methods == expected


@pytest.mark.parametrize("value", [
    [],
    ["TRACE"],
    ["GET "],
    ["get;"],
    ["G\nET"],
    [1],
    None,
    "GET",
    {},
])
def test_rejects_methods(value):
    with pytest.raises(ManifestError, match="methods"):
        parse_public({"paths": ["/a"], "methods": value})


@pytest.mark.parametrize("raw,expected", [
    (["10.0.0.0/8"], ("10.0.0.0/8",)),
    (["203.0.113.0/24", "10.0.0.0/8"], ("10.0.0.0/8", "203.0.113.0/24")),
    (["2001:0DB8:0:0::/32"], ("2001:db8::/32",)),
    (["10.0.0.0", "::1/128"], ("10.0.0.0/32", "::1/128")),
    (["10.0.0.0/8", "10.0.0.0/8"], ("10.0.0.0/8",)),
])
def test_accepts_cidrs(raw, expected):
    assert parse_public({"paths": ["/a"], "allow_cidrs": raw}).allow_cidrs == expected


@pytest.mark.parametrize("value", [
    ["10.0.0.1/8"],
    ["10.0.0.0/33"],
    ["not-a-cidr"],
    ["10.0.0.0/8;"],
    ['10.0.0.0/8"'],
    ["10.0.0.0/8\n"],
    [1],
    None,
    "10.0.0.0/8",
    {"preset": "github-hooks"},
    {},
])
def test_rejects_cidrs(value):
    with pytest.raises(ManifestError, match="allow_cidrs|preset"):
        parse_public({"paths": ["/a"], "allow_cidrs": value})


def test_golden_example():
    spec = parse_public({"paths": ["/hook/abc", "/files/*"], "methods": ["POST"]})
    assert render_public_config(spec) == r"""# managed by ingressctl: public paths only
if ($uri !~ "^(/files/.*|/hook/abc|/\.well-known/acme-challenge/.*)$") { return 404; }
set $ingressctl_method $request_method;
if ($uri ~ "^/\.well-known/acme-challenge/.*$") { set $ingressctl_method "-acme-"; }
if ($ingressctl_method !~ "^(POST|-acme-)$") { return 405; }"""


def test_golden_with_cidrs_and_two_methods():
    spec = parse_public({
        "paths": ["/api/*"],
        "methods": ["post", "GET"],
        "allow_cidrs": ["2001:db8::/48", "10.0.0.0/8"],
    })
    assert render_public_config(spec) == r"""# managed by ingressctl: public paths only
if ($uri !~ "^(/api/.*|/\.well-known/acme-challenge/.*)$") { return 404; }
set $ingressctl_method $request_method;
if ($uri ~ "^/\.well-known/acme-challenge/.*$") { set $ingressctl_method "-acme-"; }
if ($ingressctl_method !~ "^(GET|POST|-acme-)$") { return 405; }
allow 10.0.0.0/8;
allow 2001:db8::/48;
deny all;"""


def test_render_escapes_every_dot_in_paths():
    text = render_public_config(parse_public({"paths": ["/a.b.c", "/x.y/*"]}))
    assert r"/a\.b\.c" in text
    assert r"/x\.y/.*" in text
    assert "/a.b.c" not in text


def test_shuffled_input_renders_identically():
    first = parse_public({
        "paths": ["/b", "/a/*", "/b"],
        "methods": ["post", "get", "POST"],
        "allow_cidrs": ["2001:db8::/32", "10.0.0.0/8", "10.0.0.0/8"],
    })
    second = parse_public({
        "allow_cidrs": ["10.0.0.0/8", "2001:db8::/32"],
        "methods": ["GET", "POST"],
        "paths": ["/a/*", "/b"],
    })
    assert first == second
    assert render_public_config(first) == render_public_config(second)


@pytest.mark.parametrize("field,value", [
    ("paths", "/a;b"),
    ("paths", "/a\nb"),
    ("paths", '/a"b'),
    ("paths", "/a'b"),
    ("paths", "/a{b"),
    ("paths", "/a}b"),
    ("methods", ["GET;"]),
    ("methods", ["GET\n"]),
    ("methods", ['GET"']),
    ("methods", ["GET}"]),
    ("allow_cidrs", ["10.0.0.0/8;"]),
    ("allow_cidrs", ["10.0.0.0/8\n"]),
    ("allow_cidrs", ['10.0.0.0/8"']),
    ("allow_cidrs", ["10.0.0.0/8}"]),
])
def test_hostile_values_are_rejected(field, value):
    raw = {"paths": ["/ok"]}
    raw[field] = value
    with pytest.raises(ManifestError, match=field):
        parse_public(raw)


def _assert_no_injection(spec: PublicSpec) -> None:
    text = render_public_config(spec)
    cidr_lines = len(spec.allow_cidrs) + 1 if spec.allow_cidrs else 0
    assert text.startswith("# managed by ingressctl: public paths only\n")
    assert text.count("{") == 3
    assert text.count("}") == 3
    assert text.count('"') == 8
    assert text.count(";") == 4 + cidr_lines
    assert len(text.splitlines()) == 5 + cidr_lines
    assert "\r" not in text
    _assert_no_nested_if(text)


def _assert_no_nested_if(text: str) -> None:
    """nginx erlaubt kein `if` in einem `if`; jede Direktive steht in einer Zeile."""
    for line in text.splitlines():
        assert line.count("if (") <= 1, line
        assert line.count("{") == line.count("}") <= 1, line


@pytest.mark.parametrize("spec", [
    parse_public({"paths": ["/a"]}),
    parse_public({"paths": ["/a", "/b/*"], "methods": ["post", "GET"], "allow_cidrs": ["10.0.0.0/8"]}),
])
def test_rendered_config_has_no_nested_if(spec):
    _assert_no_nested_if(render_public_config(spec))


@pytest.mark.parametrize("path", ["/a", "/a/b", "/a..b", "/a-b_c.d~e", "/files-1_2~3.txt/*", "/.well-known/other"])
def test_accepted_path_cannot_inject(path):
    _assert_no_injection(parse_public({"paths": [path], "methods": ["get", "post"], "allow_cidrs": ["10.0.0.0/8"]}))


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "get"])
def test_accepted_method_cannot_inject(method):
    _assert_no_injection(parse_public({"paths": ["/a"], "methods": [method], "allow_cidrs": ["10.0.0.0/8"]}))


@pytest.mark.parametrize("cidr", ["10.0.0.0/8", "203.0.113.0/24", "2001:db8::/32", "::1/128"])
def test_accepted_cidr_cannot_inject(cidr):
    _assert_no_injection(parse_public({"paths": ["/a"], "methods": ["post"], "allow_cidrs": [cidr]}))


def _nginx_runner() -> str | None:
    if shutil.which("nginx"):
        return "nginx"
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "info"], capture_output=True)
        if probe.returncode == 0:
            return "docker"
    return None


def _server_block(text: str) -> str:
    body = "\n".join("        " + line for line in text.splitlines())
    return f"server {{\n    listen 8080;\n    location / {{\n{body}\n        return 200;\n    }}\n}}\n"


@pytest.mark.skipif(not (shutil.which("nginx") or shutil.which("docker")), reason="needs nginx or docker to validate the rendered config")
def test_rendered_config_is_accepted_by_nginx(tmp_path):
    runner = _nginx_runner()
    if runner is None:
        pytest.skip("docker is installed but its daemon does not answer")
    text = render_public_config(parse_public({
        "paths": ["/hook/abc", "/files/*"],
        "methods": ["POST"],
        "allow_cidrs": ["10.0.0.0/8"],
    }))
    conf = tmp_path / "default.conf"
    conf.write_text(_server_block(text))
    if runner == "nginx":
        main = tmp_path / "nginx.conf"
        main.write_text(
            "worker_processes 1;\nerror_log stderr;\npid " + str(tmp_path / "nginx.pid") + ";\n"
            # nginx -t opens its default log and temp paths under /var, which an unprivileged runner may not write.
            "events {}\nhttp {\naccess_log off;\n"
            + "".join(f"{name} {tmp_path / name};\n" for name in (
                "client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path", "uwsgi_temp_path", "scgi_temp_path"))
            + _server_block(text) + "}\n"
        )
        command = ["nginx", "-t", "-p", str(tmp_path), "-c", str(main)]
    else:
        command = [
            "docker", "run", "--rm", "-v", f"{conf}:/etc/nginx/conf.d/default.conf:ro",
            "nginx:alpine", "nginx", "-t",
        ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

#!/usr/bin/env bash
# Proves that seed/ works as the repository of a new installation: copies it to an empty directory, fills the placeholders with
# sample values, adds one sample app, and runs the tools and their tests there. Nothing outside the temporary directory is touched,
# no network is needed for the tools themselves (pip installs the tests' dependencies).
#
#   scripts/check-seed.sh            uses python3 from PATH
#   KEEP=1 scripts/check-seed.sh     keeps the directory and prints where it is
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
[ -n "${KEEP:-}" ] || trap 'rm -rf "$work"' EXIT
repo=$work/repo
cp -a "$here/seed" "$repo"
cd "$repo"

step() { printf '\n== %s\n' "$*"; }

# The tools need PyYAML (the homegraf image has python3-yaml); ingressctl's dependencies bring it, with pytest for the tests.
step "python $(python3 --version | cut -d' ' -f2) and the dependencies"
python3 -m venv "$work/venv"
"$work/venv/bin/pip" install -q -e 'infra/ingress-controller[test]'
py=$work/venv/bin/python

step "placeholders"
declare -A SAMPLE=(
  [DOMAIN]=home.example [HOST_IP]=10.0.0.10 [SERVER]=main [REPOSITORY]=admin/infrastructure [REPOS_DIR]=/etc/komodo/repos
  [FORGEJO_URL]=http://forgejo:3000 [INFISICAL_URL]=http://infisical:8080 [FORGEJO_USER]=owner
  [NETWORK]=homegraf-net [CREDENTIALS_VOLUME]=homegraf-ingressctl-credentials [STATE_VOLUME]=homegraf-ingressctl-state
)
for name in "${!SAMPLE[@]}"; do sed -i "s|@@${name}@@|${SAMPLE[$name]}|g" inventory/*.yaml external/*.yaml; done
if grep -rn '@@' inventory external; then echo "an inventory file uses a placeholder the bootstrap does not know (see docs/stack.md)" >&2; exit 1; fi
echo "all placeholders filled"

step "a sample app"
mkdir -p stacks/sample
cat > stacks/sample/compose.yaml <<'EOF'
services:
  web:
    image: traefik/whoami:v1.11.0
    ports: ["8000:80"]
EOF
cat > stacks/sample/ingress.yaml <<'EOF'
apiVersion: homelab.ingress/v1
namespace: infrastructure
services:
- id: sample
  stack: sample
  lifecycle: present
  proxy:
    domains: [sample.home.example]
    upstream: {scheme: http, server: main, port: 8000}
    websockets: false
    force_https: true
  certificate: {issuer_ref: homelab-private-ca, validity_days: 90, renew_before_days: 30}
  healthcheck: {path: /, accepted_statuses: [200]}
EOF
cat >> inventory/stacks.yaml <<'EOF'
- id: sample
  namespace: infrastructure
  path: stacks/sample
  compose_files: [compose.yaml]
  komodo_stack: sample
  server: main
  project_name: sample
  status: verified
EOF
git init -q . && git remote add origin ssh://git@forge.example/admin/infrastructure.git

step "TOOLS lists what exists, and the tools name no real homelab"
tools=$(grep -v '^#' "$here/TOOLS" | grep -v '^$')
for p in $tools; do [ -e "$p" ] || { echo "TOOLS lists $p, which is not in seed/" >&2; exit 1; }; done
if grep -rInE -e '(^|[^0-9.])192\.168\.[0-9]+\.[0-9]+' -e '/home/[a-z]' --include='*.py' --include='*.yaml' --include='*.yml' --include='*.cfg' --include='*.ini' \
     --include='*.md' --include='*.sh' --include='*.toml' --exclude-dir=__pycache__ $tools; then
  echo "the lines above name a private address or a home folder: tools, templates and tests use neutral names (10.0.0.x, lab.example)" >&2
  exit 1
fi
echo "$(echo "$tools" | wc -l) tool paths, all generic"

step "the tools"
$py homelab.py --help >/dev/null && echo "homelab.py --help"
$py homelab.py stacks --json | $py -c "import sys,json; d=json.load(sys.stdin); assert [s['id'] for s in d]==['sample'], d; print('stacks --json:', d[0]['id'])"
$py homelab.py removable --json | $py -c "import sys,json; d=json.load(sys.stdin); assert d[0]['id']=='sample' and d[0]['domains']==['sample.home.example'], d; print('removable --json:', d[0]['domains'])"
$py homelab.py addons --json >/dev/null && $py homelab.py templates --json >/dev/null && echo "addons, templates"
$py homelab.py new-app --app-id demo --server main --image traefik/whoami:v1.11.0 --container-port 80 --print-compose \
  | $py -c "import sys,json; d=json.load(sys.stdin); assert 'demo' in d['compose']; print('new-app --print-compose')"

step "ingressctl validate (the stack's own proxy hosts in external/ingress.yaml, the sample app)"
"$work/venv/bin/ingressctl" validate --root . | tail -5

step "the graph"
$py infra/graph/build.py --check
# The wizard validates with stackctl before it creates anything (newapp.validate); a seed without it stopped the first create.
$py -c "
import sys
sys.path.insert(0, 'infra/homelab')
import newapp, stackctl
errors, warnings = stackctl.validate(newapp.ROOT)
assert not errors, errors
print('stackctl validate through the wizard: ok')
"
$py infra/graph/build.py | $py -c "
import sys, json
g = json.load(sys.stdin)
ids = {n['id'] for n in g['nodes']}
want = {'stack:sample', 'domain:sample.home.example', 'machine:main', 'infra:npm', 'infra:komodo', 'infra:ca', 'infra:homegraf'}
assert want <= ids, sorted(want - ids)
hardware = [n['id'] for n in g['nodes'] if n['type'] in ('isp', 'router', 'switch', 'device')]
assert not hardware, 'the hardware layer is optional, but the skeleton has one: %s' % hardware
assert not [w for w in g['warnings'] if 'dangling' in w], g['warnings']
print('graph:', len(g['nodes']), 'nodes, no hardware layer, platform and sample app present')"

step "the tests of the tools"
"$work/venv/bin/pytest" -q -p no:cacheprovider infra/graph infra/homelab infra/infisical infra/stackctl infra/ingress-controller/tests

echo
echo "seed ok${KEEP:+ (kept in $repo)}"

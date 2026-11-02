# The build context is the repository root (docker build -f app/Dockerfile .); Dockerfile.dockerignore keeps it to app/, seed/ and TOOLS.
FROM node:26.13.0-slim AS build
WORKDIR /app
COPY app/package.json app/package-lock.json ./
RUN npm ci
COPY app/tsconfig.server.json app/tsconfig.web.json app/vite.config.ts ./
COPY app/src src
RUN npm run build && npm prune --omit=dev

# The command-line tools (the paths listed in TOOLS, without their tests): the app runs them against the repository it is
# given, and `docker exec homegraf homelab.py ...` runs them by hand.
FROM node:26.13.0-slim AS tools
COPY TOOLS /ctx/TOOLS
COPY seed /ctx/seed
RUN mkdir /out && cd /ctx/seed \
 && for p in $(grep -v '^#' /ctx/TOOLS); do mkdir -p "/out/$(dirname "$p")" && cp -a "$p" "/out/$p"; done \
 && find /out \( -name __pycache__ -o -name .pytest_cache -o -name tests \) -prune -exec rm -rf {} + \
 && find /out -name 'test_*.py' -delete

# One container: Hono serves the API and the built React app; python3 runs infra/graph/build.py from the repo
# checkout mounted at /repo, git is for the PR flow.
FROM node:26.13.0-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-yaml git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# The image only runs node: npm, npx and corepack (with their own dependencies, which scanners flag) are build tools.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /opt/yarn-*
WORKDIR /app
# TOOLS_DIR: where the tools are; HOMELAB_ROOT: the repository they work on unless told otherwise (the read-only checkout).
ENV NODE_ENV=production REPO_DIR=/repo WEB_DIR=dist/web PYTHONDONTWRITEBYTECODE=1 TOOLS_DIR=/opt/homegraf HOMELAB_ROOT=/repo
COPY --from=build /app/node_modules node_modules
COPY --from=build /app/dist dist
COPY app/package.json .
COPY --from=tools /out /opt/homegraf
RUN ln -s /opt/homegraf/homelab.py /usr/local/bin/homelab.py
# Writable clone of the repository for "New app" and the users file (volumes; Docker copies this ownership into them).
RUN mkdir /work /data && chown node:node /work /data
USER node
EXPOSE 8080
CMD ["node", "dist/server/index.js"]

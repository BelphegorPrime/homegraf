FROM node:26.13.0-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.server.json tsconfig.web.json vite.config.ts ./
COPY src src
RUN npm run build && npm prune --omit=dev

# One container: Hono serves the API and the built React app; python3 runs infra/graph/build.py from the repo
# checkout mounted at /repo, git is for the PR flow.
FROM node:26.13.0-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-yaml git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# The image only runs node: npm, npx and corepack (with their own dependencies, which scanners flag) are build tools.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /opt/yarn-*
WORKDIR /app
ENV NODE_ENV=production REPO_DIR=/repo WEB_DIR=dist/web PYTHONDONTWRITEBYTECODE=1
COPY --from=build /app/node_modules node_modules
COPY --from=build /app/dist dist
COPY package.json .
# Writable clone of the repository for "New app" and the users file (volumes; Docker copies this ownership into them).
RUN mkdir /work /data && chown node:node /work /data
USER node
EXPOSE 8080
CMD ["node", "dist/server/index.js"]

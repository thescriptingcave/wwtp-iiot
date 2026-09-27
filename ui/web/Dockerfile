# syntax=docker/dockerfile:1.7
#
# The custom dashboard. Next.js, built once at image-build time and run with
# `next start`, rather than run in development mode.
#
# This is the third Dockerfile in the project and it is deliberately *not* a
# variation of the Python one. Two ecosystems, two build systems, two sets of
# opinions — and merging them into a single "base" image would produce something
# with 800 MB of node_modules and a Python virtualenv, neither of which the other
# half needs.

# ─── deps ─────────────────────────────────────────────────────────────────────
# Pinned by tag; see the note in docker/Dockerfile.python and docs/SECURITY.md
# about tag-versus-digest. The Next.js version is pinned exactly rather than with
# a caret, because a caret here means the image you tested is not the image you
# deploy.
FROM node:22.14-alpine AS deps

WORKDIR /app
# `npm ci` and not `npm install`: it installs from package-lock.json and *fails*
# if the lock and the manifest disagree. `npm install` quietly updates the lock,
# so a broken lockfile becomes a silently different build instead of an error.
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --ignore-scripts

# ─── build ────────────────────────────────────────────────────────────────────
FROM node:22.14-alpine AS build

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Baked in at build time: only `NEXT_TELEMETRY_DISABLED`, which is not a secret
# and is a build-time decision rather than a run-time one.
#
# **The two `NEXT_PUBLIC_POSTGRES_*` arguments this file used to take are gone**,
# and the reason is worth the two lines. `NEXT_PUBLIC_` means "baked into the
# JavaScript the browser downloads", which was fine for a host and a port and is
# catastrophically wrong for a password. The comment below this used to say "the
# browser cannot keep a secret" while the compose file handed it a database URL —
# only a mistake away from handing it a credential.
#
# The connection details are now runtime environment variables read by
# `lib/db.ts`, which is marked `server-only` and throws at build time if a client
# component imports it. `docs/SECURITY.md` has the threat model.
ARG NEXT_TELEMETRY_DISABLED=1
ENV NEXT_TELEMETRY_DISABLED=$NEXT_TELEMETRY_DISABLED

# `npm ci` without --omit=dev here: the Next.js compiler and its plugins are
# devDependencies and are needed to build. The *runtime* image above does not
# carry them, which is the point of the split.
RUN npm ci --ignore-scripts && npm run build

# ─── runtime ──────────────────────────────────────────────────────────────────
FROM node:22.14-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# A container that runs as root is a container where one RCE is a host root.
# The node image already has a `node` user; this just stops using root.
RUN addgroup -g 10002 -S app && adduser -u 10002 -S app -G app

WORKDIR /app
# Only the production dependency tree and the built output. No sources, no
# devDependencies, no lockfile.
COPY --from=deps --chown=app:app /app/node_modules ./node_modules
COPY --from=build --chown=app:app /app/.next ./.next
COPY --from=build --chown=app:app /app/public ./public
COPY --from=build --chown=app:app /app/package.json ./package.json
COPY --from=build --chown=app:app /app/next.config.* ./ 2>/dev/null || true

USER 10002:10002
EXPOSE 3000

# The healthcheck hits the app's own endpoint rather than the TCP port: an open
# port proves the process bound a socket, not that it can answer. Those are
# different failures and only one of them matters to a user.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD node -e "fetch('http://localhost:3000/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["npx", "next", "start", "-p", "3000"]

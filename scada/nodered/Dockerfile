# ═════════════════════════════════════════════════════════════════════════════
# Node-RED for the operator flows.
#
# Built on the official image rather than a node:alpine base with npm install in
# it. Three reasons, and the first is the only one that matters:
#
#   1. **It is the same runtime the flows were authored against.** A hand-rolled
#      image pins nothing by accident and drifts on the first `npm install`.
#   2. It has the `tini` init, so Node-RED's SIGTERM handling actually runs and
#      `docker compose down` stops it cleanly instead of waiting for the 10-second
#      kill.
#   3. It is a maintained artefact. Depending on somebody else's Dockerfile to
#      keep working is cheaper than maintaining your own.
#
# The flows are NOT baked in. They are mounted from `scada/flows/` at run time
# (see compose.yaml) so `python -m scada.build_flows` followed by a reload is
# enough, and so the generated files in git are the same ones that run.
#
# The image is `nodered/node-red`, which is the name people reach for and which
# does not exist — `node-red` on Docker Hub is a different, unmaintained project.
# Finding that out cost a pull.
# ═════════════════════════════════════════════════════════════════════════════
FROM nodered/node-red:4.0.9

# The two non-core nodes, installed explicitly.
#
# The obvious thing is to rely on the base image's own `COPY package.json` +
# `npm install` step, and that is what its documentation implies. It does not
# work, because **the base image is prebuilt**: its `npm install` already ran
# against *its* package.json, at the time the base image was built, and a
# `package.json` added to your build context afterwards is never read.
#
# The symptom is the sharpest one Node-RED has:
#
#     [info] Waiting for missing types to be registered:
#     [info]  - postgresql
#     [info]  - modbus-write
#
# and then it simply sits there. No error, no exit, no timeout — the runtime is
# up, the editor would load, and every node on the flows is a red box waiting for
# a type that will never arrive. A flow that imports and shows red boxes is the
# Node-RED equivalent of a stack that starts and then logs errors, and this is
# exactly the case.
#
# `npm install --omit=dev` into `/data`, which is where the runtime looks, and
# the versions are pinned in `package.json` beside this file.
COPY package.json /data/package.json
USER root
RUN cd /data && npm install --omit=dev --no-audit --no-fund
USER node-red

# `settings.js` for the settings the image's environment variables do not cover
# — chiefly `functionExternalModules: {}`, which is a real control and not a
# preference. `ADMIN_ROOT` stays an environment variable because putting it here
# as well would be two places to change.
#
# **To `/data`, and the `CMD` below points Node-RED at `/data` as its user
# directory.** Both halves are load-bearing and neither works alone.
#
# The base image's user directory is `/usr/src/node-red/.node-red`, *not* `/data`.
# The container says so on every boot:
#
#     [info] Settings file  : /usr/src/node-red/.node-red/settings.js
#     [info] User directory : /usr/src/node-red/.node-red
#     [info] Flows file     : /usr/src/node-red/.node-red/flows.json
#     [info] Creating new flow file
#
# That is the image's own stock `settings.js` — 24 kB of commented defaults, not
# the 2 kB file beside this one — so every setting in ours was silently ignored.
# The symptom is the quietest this project produces, because the runtime reports
# health while doing nothing at all:
#
#   * the admin root stayed `/`, so `httpAdminRoot` never applied and the editor
#     answered on `/red` while `ADMIN_ROOT=/scada` made `/scada` a 404;
#   * `flowFile` resolved against the *other* directory, so the flows
#     `entrypoint.sh` assembles into `/data/flows.json` were never read and the
#     runtime started with **zero** nodes — `Creating new flow file`, then
#     `Started flows`, and an operator looking at an empty editor has no way to
#     tell that from a Node-RED with nothing deployed;
#   * `node-red-contrib-postgresql` and `-modbus` are installed to `/data`, and
#     `/usr/src/node-red/.node-red/node_modules` is empty, so loading a real flow
#     produces the "Waiting for missing types to be registered" stall documented
#     above — the very symptom the `npm install` into `/data` was meant to prevent.
#
# `--userDir /data` in the `CMD` is what closes all three: Node-RED then reads
# `/data/settings.js`, resolves `flowFile: 'flows.json'` against `/data`, and
# finds `/data/node_modules`. The base image has no `USER_DIR` environment
# variable, so this has to be an argument.
COPY settings.js /data/settings.js

# The credential is derived from the environment rather than committed and not
# typed into the editor. See `entrypoint.sh` for why both of those are wrong for
# this project.
#
# **At the end, after `USER node-red`, deliberately** — it overrides the base
# image's own `ENTRYPOINT ["./entrypoint.sh"]`, so it has to be the last
# instruction that touches it or the base's wins. The first version of this file
# put the block earlier and the credential was silently never written, which
# looks exactly like "the entrypoint is not being run".
COPY entrypoint.sh /usr/local/bin/wwtp-entrypoint.sh
# As root, because the `npm install` above left the build as `node-red` and
# `/usr/local/bin` is not writable by it. The failure is a bare
# `chmod: cannot access ... Operation not permitted`, which says nothing about
# which line of which Dockerfile is at fault.
USER root
RUN chmod +x /usr/local/bin/wwtp-entrypoint.sh

USER node-red

ENV NR_DATA_DIR=/data
ENTRYPOINT ["/usr/local/bin/wwtp-entrypoint.sh"]
CMD ["node-red", "--userDir", "/data"]

#!/bin/sh
# Generate Node-RED credentials from the environment, then start.
#
# ── why this exists ──────────────────────────────────────────────────────────
# The PostgreSQL and Modbus nodes need a *credential*, and a credential is a
# secret. The two obvious ways to supply one are both wrong for this project:
#
#   * commit `flows_cred.json` — puts the database password in git history, and
#     this project has already had a conversation about a shared password owning
#     the whole database. It would undo that, silently, in a file nobody reads.
#
#   * "open the editor and add it" — a manual step, so a `docker compose up` on a
#     clean checkout produces a runtime full of red boxes and no explanation. The
#     flow loads, the nodes exist, and every query fails with
#     `node.config.pgPool.connect is not a function`, which is a Node-RED
#     internal and tells an operator nothing at all.
#
# So: the credential is derived from the environment, written once, and gitignored.
# An existing `flows_cred.json` is never overwritten, so a credential entered in
# the editor survives a restart.
#
# Deliberately POSIX `sh` and deliberately quiet. This runs before anything else
# and its only job is to not be interesting.
set -eu

DATA_DIR="${NR_DATA_DIR:-/data}"
CRED_FILE="$DATA_DIR/flows_cred.json"
FLOWS_DIR="${NR_FLOWS_DIR:-/flows}"

# ── assemble the flows ───────────────────────────────────────────────────────
# Node-RED loads exactly one `flows.json`. The generator writes one file per flow
# — `01-mimic.json`, `02-annunciator.json`, `03-control.json` — because they are
# built by three independent functions and importing one of them should not
# import the other two.
#
# They are three *tabs* in one runtime, and concatenating them is what makes the
# `global` context shared. Node-RED's `global` is per flow *file*, so loading
# them as three files would mean three copies of the tag list and a `?? []`
# default quietly emptying two of them.
#
# Done in `sh` because this is the only place it happens and it is four lines.
# It cannot be `cat` — the files are JSON *arrays*, and three arrays concatenated
# is not a JSON document.
assemble_flows() {
    out="$DATA_DIR/flows.json"

    # Concatenated with `node`, not with `sed`.
    #
    # The first version of this was a `sed` pipeline that stripped the leading
    # `[` and the trailing `]` and joined with commas, and it produced
    # `Extra data: line 212 column 5` — invalid JSON, from a shell script, in a
    # container, at deploy time. `sed` cannot do this correctly because JSON
    # arrays have no line structure to key off: with `indent=4` the opening `[` and
    # the closing `]` are on their own lines *sometimes*, and the interesting case
    # is when a node is long enough to wrap.
    #
    # `node` is already in the image — it is the runtime — so this costs nothing
    # and the output is valid JSON by construction rather than by careful quoting.
    # A `sed` pipeline that quietly produces invalid JSON gives a runtime that
    # starts cleanly and deploys nothing, which is the same shape as every other
    # "it came up" failure in this project.
    #
    # `tags.json` is excluded by name: it is a tag list, not a flow, and
    # concatenating it would produce a document that is valid JSON and means
    # nothing.
    # **Written to a temporary and renamed into place**, because the base image
    # ships a root-owned `/data/flows.json` and the container runs as `node-red`.
    #
    #     Error: EACCES: permission denied, open '/data/flows.json'
    #
    # The *directory* `/data` is owned by `node-red`, so a new file can be created
    # and an existing one can be replaced by renaming over it — but the shipped
    # file is `-rw-r--r-- root`, so opening it for writing needs permission on
    # the file and fails. Renaming does not.
    #
    # Which is the sixth time in this project that a thing looked like it worked
    # and the only difference was ownership. A generated file that cannot replace
    # the generated file is a runtime that starts, serves the base image's
    # example flow, and reports no error at all.
    node -e '
      const fs = require("fs"), path = require("path");
      const dir = process.argv[1], out = process.argv[2];
      const files = fs.readdirSync(dir)
        .filter((f) => f.endsWith(".json") && f !== "tags.json")
        .sort();
      if (files.length === 0) {
        console.error("nodered: no flows found in " + dir);
        process.exit(1);
      }
      const nodes = files.flatMap((f) => JSON.parse(
        fs.readFileSync(path.join(dir, f), "utf8")));
      fs.writeFileSync(out + ".new", JSON.stringify(nodes, null, 4) + "\n");
      console.log("nodered: assembled " + out + " from " + files.length
        + " file(s), " + nodes.length + " nodes");
    ' "$FLOWS_DIR" "$out"

    # The rename is the part that needs the permissions, and it is also what makes
    # the replace atomic: a Node-RED restart cannot see a half-written document.
    mv -f "$out.new" "$out"
}

# Before the credential: a runtime with no flows starts cleanly and shows an
# empty editor, which looks like a working Node-RED with nothing deployed.
assemble_flows

if [ ! -f "$CRED_FILE" ]; then
    # `POSTGRES_PASSWORD` is required. Without it there is nothing to write, and
    # failing here with a clear message beats starting a runtime whose every query
    # is going to fail for a reason it will not report.
    : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required to build the Node-RED database credential}"

    NODE_RED_DB_HOST="${POSTGRES_HOST:-db}"
    NODE_RED_DB_PORT="${POSTGRES_PORT:-5432}"
    NODE_RED_DB_NAME="${POSTGRES_DB:-wwtp}"
    NODE_RED_DB_USER="${NODE_RED_DB_USER:-$POSTGRES_USER}"

    echo "nodered: writing $CRED_FILE for $NODE_RED_DB_USER@$NODE_RED_DB_HOST:$NODE_RED_DB_PORT/$NODE_RED_DB_NAME"

    # `credentialSecret` is what Node-RED encrypts `flows_cred.json` *with*. It
    # is generated once and kept, so a restart can still read what it wrote. It
    # lives beside the credential it protects, which is honest: this file is not
    # a secret store, it is "don't check this into git".
    if [ ! -f "$DATA_DIR/.credentialSecret" ]; then
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' \
            > "$DATA_DIR/.credentialSecret"
        chmod 600 "$DATA_DIR/.credentialSecret"
    fi
    CREDENTIAL_SECRET="$(cat "$DATA_DIR/.credentialSecret")"

    # Written as JSON, by hand, with a small helper for the escaping that actually
    # matters. A password containing a quote or a backslash would otherwise
    # produce a credential file Node-RED cannot parse, and the error is a JSON
    # parse failure in a log line about a file the reader did not know existed.
    json_escape() {
        printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
    }

    {
        printf '{"%s":{\n' "$(json_escape "$CREDENTIAL_SECRET")"
        printf '  "wwtp-db": {\n'
        printf '    "id": "wwtp-db",\n'
        printf '    "name": "wwtp-db",\n'
        printf '    "type": "postgresdb",\n'
        printf '    "postgresqldb": {\n'
        printf '      "host": "%s",\n' "$(json_escape "$NODE_RED_DB_HOST")"
        printf '      "port": "%s",\n' "$NODE_RED_DB_PORT"
        printf '      "database": "%s",\n' "$(json_escape "$NODE_RED_DB_NAME")"
        printf '      "user": "%s",\n' "$(json_escape "$NODE_RED_DB_USER")"
        printf '      "password": "%s",\n' "$(json_escape "$POSTGRES_PASSWORD")"
        printf '      "ssl": false,\n'
        printf '      "applicationName": "wwtp-scada"\n'
        printf '    }\n'
        printf '  },\n'
        printf '  "wwtp-softplc-modbus": {\n'
        printf '    "id": "wwtp-softplc-modbus",\n'
        printf '    "name": "wwtp-softplc-modbus",\n'
        printf '    "type": "modbus-client",\n'
        printf '    "modbus-client": {\n'
        printf '      "host": "%s",\n' "$(json_escape "${MODBUS_HOST:-softplc}")"
        printf '      "port": %s,\n' "${MODBUS_PORT:-5020}"
        printf '      "unit_id": %s,\n' "${MODBUS_UNIT_ID:-1}"
        printf '      "serverType": "tcp",\n'
        printf '      "reconnectDelay": "1000",\n'
        printf '      "reconnectTries": "10"\n'
        printf '    }\n'
        printf '  }\n'
        # The secret map, then the document.
        #
        # Four opens -- the document, the secret map, and two credentials -- and
        # this is the one place they are closed, so the count is written out
        # rather than trusted. The first version closed three, and Node-RED's
        # failure was `Expecting ',' delimiter: line 30 column 1`: a JSON parse
        # error, in a log line, about a file the reader did not know existed.
        printf '}\n'
        printf '}\n'
    } > "$CRED_FILE"
    chmod 600 "$CRED_FILE"
fi

exec "$@"
SHEEOF
chmod +x scada/nodered/entrypoint.sh
.venv/bin/python - <<'PYEOF'
import pathlib
p = pathlib.Path("scada/nodered/Dockerfile"); s = p.read_text()
s = s.replace('''USER node-red''','''# The credential is derived from the environment rather than committed or typed
# into the editor. See `entrypoint.sh` for why both of those are wrong here.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENV NR_DATA_DIR=/data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["node-red"]

USER node-red''')
p.write_text(s)
# and the settings need credentialSecret read from the file the entrypoint writes
p = pathlib.Path("scada/nodered/settings.js"); s = p.read_text()
s = s.replace("""module.exports = {""","""const fs = require('fs');
const path = require('path');

/** The key `flows_cred.json` is encrypted with, read from the file the
 *  entrypoint generates.
 *
 *  Node-RED generates one itself and logs a warning that it is "system-generated"
 *  and unrecoverable, which is true and unhelpful: it means the credential this
 *  project deliberately does not commit cannot survive a volume rebuild, and
 *  nobody is told what to do about it. Reading it from a file we generate is one
 *  line and removes the warning.
 *
 *  `credentialSecret` is a *key*, not a password. It sits beside the credential
 *  it protects; the thing that is kept out of git is the whole `flows_cred.json`
 *  (see .gitignore).
 */
function credentialSecret() {
    const file = path.join(process.env.NR_DATA_DIR || '/data', '.credentialSecret');
    try {
        return fs.readFileSync(file, 'utf8').trim();
    } catch {
        return undefined;   // let Node-RED generate one and warn, as it would anyway
    }
}

module.exports = {
    credentialSecret: credentialSecret(),
""")
p.write_text(s); print("ok")
PYEOF
docker build -t wwtp-nodered-test -f scada/nodered/Dockerfile scada/nodered 2>&1 | tail -2
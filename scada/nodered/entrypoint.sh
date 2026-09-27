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
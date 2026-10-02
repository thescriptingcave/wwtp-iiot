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
    : "${POSTGRES_USER:?POSTGRES_USER is required to build the Node-RED database credential}"

    # The host, port and database are *not* written here. They live in the
    # `postgreSQLConfig` node in `flows.json`, which reads them from the
    # environment by name (`hostFieldType: env` and friends), so there is one place
    # that knows the address -- compose.yaml -- rather than two that have to agree.
    # Only the user and password are credentials, and they are the only two things
    # that cannot be in a flow file.
    echo "nodered: writing $CRED_FILE for $POSTGRES_USER from $DATA_DIR/flows.json"

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

    # Written with `node`, in the shape `credentials.js` actually reads, and for the
    # same reason the flow assembly above uses `node`: this file has a required
    # shape that is not obvious, and hand-assembling it in `printf` is how you get
    # a JSON parse error in a log line about a file the reader did not know existed.
    #
    # **The shape is not "an object keyed by the secret".** Node-RED derives
    # `sha256(credentialSecret)` as the AES key, and stores the credentials under a
    # single literal `$` key whose value is a 16-byte initialisation vector in hex
    # followed by the AES-256-CTR ciphertext in base64. Nothing documents that
    # except `credentials.js`, and getting it wrong does not fail loudly — with the
    # wrong key *name* Node-RED logs
    #
    #     [warn] Encrypted credentials not found
    #
    # adopts the raw file as its credential cache, and every lookup misses. That
    # surfaces three nodes later, in the runtime log, as
    #
    #     TypeError: node.config.pgPool.connect is not a function
    #
    # which is a Node-RED internal and tells an operator nothing whatsoever. That
    # is precisely the symptom the top of this file exists to prevent, so the shape
    # is written out here rather than left to be rediscovered.
    #
    # The password comes from the environment rather than an argument, so it does
    # not appear in `ps` for the lifetime of this process.
    #
    # **Inside, the credential is keyed by the config node's *id*, not by a
    # credential name.** `credentials.get(id)` is called with the node id, so a
    # credential filed under any other key is present in the file and unread at
    # run time. The id is read out of the flow assembled just above rather than
    # written here, because the id that has to agree is the one
    # `scada/build_flows.py` invents, and two places inventing an id is how they
    # drift apart.
    node -e '
      const crypto = require("crypto"), fs = require("fs");
      const [ , out, secret, flows, configType ] = process.argv;

      const flow = JSON.parse(fs.readFileSync(flows, "utf8"));
      const targets = flow.filter((n) => n.type === configType);
      if (targets.length !== 1) {
        console.error(
          `expected exactly one ${configType} node in ${flows}, found `
          + `${targets.length}. The database credential is stored under that `
          + `node id, so with none there is nothing to attach it to.`);
        process.exit(1);
      }

      const creds = {};
      creds[targets[0].id] = {
        user: process.env.POSTGRES_USER,
        password: process.env.POSTGRES_PASSWORD,
      };

      const key = crypto.createHash("sha256").update(secret).digest();
      const iv = crypto.randomBytes(16);
      const cipher = crypto.createCipheriv("aes-256-ctr", key, iv);
      const body = cipher.update(JSON.stringify(creds), "utf8", "base64")
                 + cipher.final("base64");
      fs.writeFileSync(
        out,
        JSON.stringify({ "$": iv.toString("hex") + body }) + "\n",
        { mode: 0o600 });
    ' "$CRED_FILE" "$CREDENTIAL_SECRET" \
      "$DATA_DIR/flows.json" "postgreSQLConfig"

    chmod 600 "$CRED_FILE"
fi

# `exec`, so Node-RED *is* this process: it receives the signal `docker compose
# down` sends, and the `tini` init above reaps what it leaves behind. Anything
# after this line is dead — `exec` does not return.
#
# `credentialSecret` is not handled here. It used to be, by a Python block that
# rewrote `settings.js` at build time to inject the function; that block sat after
# this `exec`, so it never ran, and the setting it was adding to `settings.js` was
# never there. The block has been removed and the function now lives in
# `settings.js` itself, reading `$NR_DATA_DIR/.credentialSecret` at startup — which
# is what it should have been doing all along, and needs no build step to do it.
exec "$@"

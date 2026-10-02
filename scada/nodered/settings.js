/**
 * Node-RED settings, and only the ones that matter here.
 *
 * Everything else is Node-RED's own default. An explicit settings.js is a
 * file someone will later add a secret to, and a settings.js that is a wall of
 * commented-out defaults is a file nobody reads.
 */
const fs = require('fs');
const path = require('path');

/**
 * The key `flows_cred.json` is encrypted with, read from the file the entrypoint
 * generates at `$NR_DATA_DIR/.credentialSecret`.
 *
 * Node-RED generates one itself and logs a warning that it is "system-generated"
 * and unrecoverable, which is true and unhelpful: it means the credential this
 * project deliberately does not commit cannot survive a volume rebuild, and
 * nobody is told what to do about it. Reading it from a file we generate is one
 * line and removes the warning.
 *
 * Returning `undefined` when the file is absent is deliberate — that is Node-RED's
 * own behaviour, and it is the right one: generate a key and warn rather than
 * refuse to start.
 *
 * `credentialSecret` is a *key*, not a password. It sits beside the credential it
 * protects; the thing kept out of git is the whole `flows_cred.json`
 * (see .gitignore).
 */
function credentialSecret() {
    const file = path.join(process.env.NR_DATA_DIR || '/data', '.credentialSecret');
    try {
        return fs.readFileSync(file, 'utf8').trim();
    } catch {
        return undefined;
    }
}

module.exports = {
    credentialSecret: credentialSecret(),

    /** Where the flow files are mounted from. `flows_cred.json` is the encrypted
     *  credentials file, which is why the directory is not read-only — without
     *  write access Node-RED cannot save the credentials it generates on first
     *  boot, and it exits with a message that reads like a permissions bug.
     *
     *  Relative on purpose, and only correct because the image starts Node-RED with
     *  `--userDir /data` (see `Dockerfile`). Relative to the base image's default
     *  user directory this resolves to `/usr/src/node-red/.node-red/flows.json`,
     *  which nothing ever writes, and the runtime starts with zero nodes. If the
     *  user directory ever moves, this has to move with it. */
    flowFile: 'flows.json',

    /** The editor's admin route, moved off `/` so it cannot be confused with
     *  anything the plant serves. Set by `ADMIN_ROOT` in compose. */
    httpAdminRoot: process.env.ADMIN_ROOT || '/scada',

    httpAdminAuth: undefined,   // set by the editor on first run; see .gitignore

    /* **There is no switch that disables the Node-RED editor.**
     *
     * There used to be a `NODE_RED_EDITOR` environment variable in compose.yaml
     * for this, with comments here and in the Dockerfile explaining that the image
     * maps it, and `tests/test_grafana_dashboards.py` asserting it was set to
     * "false". Nothing read it -- no Node-RED setting consumes it, no contrib node
     * reads it, and the `nodered/node-red` image has no such variable. So the
     * editor was on, and a test was confirming that a variable meant to turn it
     * off existed.
     *
     * What is actually true, and worth knowing:
     *
     *   * the editor is reachable at `/scada/`, on `127.0.0.1` only, because
     *     compose binds the published port to loopback. It is not on the network.
     *   * deploying from it writes `$NR_DATA_DIR/flows.json`, which is the file the
     *     entrypoint *assembled* at startup and the one the runtime has loaded. So
     *     a deploy overwrites the concatenation with whatever the editor held, and
     *     `python -m scada.build_flows --check` then fails against the committed
     *     files for a reason nothing explains.
     *   * `scada/README.md` covers that, and the drift gate is the detector.
     *
     * To read or explore without risking that, copy the flows out of a running
     * container rather than deploying into it. There is no supported way to make
     * the editor read-only, and inventing one here would be a private fork of
     * Node-RED's own admin app for no gain over the loopback bind. */

    /** Function nodes are *not* allowed external modules. The flows use no
     *  `require()` at all — deliberately, since a generated flow that could
     *  `require` anything would be a generated flow that could break in a way
     *  no test in this repository can see. */
    functionGlobalContext: {},
    functionExternalModules: {},

    logging: {
        console: {
            level: process.env.LOG_LEVEL || 'info',
            metrics: false,
            audit: false,
        },
    },
};

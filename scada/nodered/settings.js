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

    /* The editor is disabled by `NODE_RED_EDITOR=false` in compose.yaml, not
     * here, because the image maps that variable and duplicating it in two places
     * is two places to change.
     *
     * The reason it is off: the flows in this repository are *generated* from
     * `contracts/tags.yaml`. A hand-edited flow is a change that
     * `python -m scada.build_flows` silently reverts, and a silent revert of an
     * operator's flow is worse than no editor at all.
     *
     * Set `NODE_RED_EDITOR=true` to open it when you want to explore. Anything
     * you build there is a scratch pad until you move it into
     * `scada/build_flows.py` and regenerate. */

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

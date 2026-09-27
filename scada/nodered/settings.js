/**
 * Node-RED settings, and only the ones that matter here.
 *
 * Everything else is Node-RED's own default. An explicit settings.js is a
 * file someone will later add a secret to, and a settings.js that is a wall of
 * commented-out defaults is a file nobody reads.
 */
module.exports = {
    /** Where the flow files are mounted from. `flows_cred.json` is the encrypted
     *  credentials file, which is why the directory is not read-only — without
     *  write access Node-RED cannot save the credentials it generates on first
     *  boot, and it exits with a message that reads like a permissions bug. */
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

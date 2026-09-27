/**
 * Next.js configuration.
 *
 * Three settings, and the second one is the only reason this file exists.
 */

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,

  /**
   * `output: 'standalone'` is **deliberately not set.**
   *
   * It would produce a self-contained server with its own pruned
   * `node_modules`, which is the right answer for most deployments — and the
   * wrong one here, because `ui/web/Dockerfile` already splits the build
   * from the runtime and copies a pruned dependency tree across itself. Turning
   * it on as well gives two pruning mechanisms, and the question of which one
   * dropped a module is answerable only by deploying.
   *
   * The Dockerfile is the thing that should be changed if the image grows, and it
   * is one file in one place.
   */

  /**
   * No `experimental.serverActions`, and therefore no `bodySizeLimit`.
   *
   * The first version of this file set `bodySizeLimit: '0kb'` to make "this app
   * has no server actions" a build error rather than a comment. Next rejects it:
   *
   *     Server Actions Size Limit must be a valid number or filesize format
   *     larger than 1MB
   *
   * So the guarantee is now the other way round — the setting is absent, and the
   * only acknowledgement of the policy is a comment plus a test that greps for
   * `'use server'`. Which is weaker, and honestly so: `bodySizeLimit` was never
   * a way to *disable* server actions, only to cap their request size, and a
   * setting that reads like an off switch and is not is worse than no setting.
   *
   * The policy itself: **the only write path is the Node-RED annunciator**, which
   * owns the credential and the audit trail. A second write path on an
   * unauthenticated page would be a way for anyone who can load it to silence an
   * alarm, which is the one thing an alarm panel must not be.
   */

  // Nothing here is a static asset, so no image config and no loader.
  poweredByHeader: false,

  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          // This page shows plant data to whoever can load it. It has no
          // authentication, so the browser is told not to cache it, not to
          // embed it, and not to send a referrer anywhere.
          { key: 'Cache-Control', value: 'no-store' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'Referrer-Policy', value: 'no-referrer' },
        ],
      },
    ];
  },
};

export default config;

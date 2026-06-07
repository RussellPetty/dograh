import { withSentryConfig } from "@sentry/nextjs";
import type { NextConfig } from "next";

// Origins allowed to embed Viato Voice (the Viato CRM host) and use the
// microphone for in-browser test calls. Override via env per deployment.
// The env values are SPACE-SEPARATED ORIGINS (no quotes) so they survive env
// stores that escape quotes (e.g. Coolify mangles `'self'` → `\'self\'`). The
// `'self'` keyword and the Permissions-Policy quotes are added here, not in env.
const EMBED_FRAME_ANCESTORS = `'self' ${
  process.env.EMBED_FRAME_ANCESTORS || "https://viato.ai https://*.viato.ai"
}`.trim();
// Permissions-Policy allowlists must be explicit origins (no wildcards), each quoted.
const EMBED_MIC_ALLOWLIST = [
  "self",
  ...(process.env.EMBED_MIC_ALLOWLIST || "https://viato.ai https://app.viato.ai")
    .split(/\s+/)
    .filter(Boolean)
    .map((o) => `"${o}"`),
].join(" ");

const nextConfig: NextConfig = {
  /* config options here */
  output: 'standalone',
  experimental: {
    serverSourceMaps: true,
  },
  async headers() {
    return [
      {
        // Allow Viato CRM to embed Viato Voice in an iframe and grant the
        // microphone for browser-based test calls. No X-Frame-Options is set,
        // so framing is governed solely by the CSP frame-ancestors directive.
        source: "/:path*",
        headers: [
          {
            key: "Content-Security-Policy",
            value: `frame-ancestors ${EMBED_FRAME_ANCESTORS};`,
          },
          {
            key: "Permissions-Policy",
            value: `microphone=(${EMBED_MIC_ALLOWLIST}), camera=()`,
          },
        ],
      },
    ];
  },
  async rewrites() {
    return [
      {
        source: "/ingest/static/:path*",
        destination: "https://us-assets.i.posthog.com/static/:path*",
      },
      {
        source: "/ingest/:path*",
        destination: "https://us.i.posthog.com/:path*",
      },
      {
        source: "/ingest/decide",
        destination: "https://us.i.posthog.com/decide",
      },
    ];
  },
  // This is required to support PostHog trailing slash API requests
  skipTrailingSlashRedirect: true,
};

export default withSentryConfig(nextConfig, {
  // For all available options, see:
  // https://www.npmjs.com/package/@sentry/webpack-plugin#options

  org: "dograh",
  project: "javascript-nextjs",

  // Only print logs for uploading source maps in CI
  silent: !process.env.CI,

  // For all available options, see:
  // https://docs.sentry.io/platforms/javascript/guides/nextjs/manual-setup/

  // Upload a larger set of source maps for prettier stack traces (increases build time)
  widenClientFileUpload: true,

  // Route browser requests to Sentry through a Next.js rewrite to circumvent ad-blockers.
  // This can increase your server load as well as your hosting bill.
  // Note: Check that the configured route will not match with your Next.js middleware, otherwise reporting of client-
  // side errors will fail.
  tunnelRoute: "/monitoring",

  // Automatically tree-shake Sentry logger statements to reduce bundle size
  disableLogger: true,

  // Enables automatic instrumentation of Vercel Cron Monitors. (Does not yet work with App Router route handlers.)
  // See the following for more information:
  // https://docs.sentry.io/product/crons/
  // https://vercel.com/docs/cron-jobs
  automaticVercelMonitors: true,
});

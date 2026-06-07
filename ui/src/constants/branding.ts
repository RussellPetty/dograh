/**
 * Central brand constants for the "Viato Voice" deployment.
 *
 * Single source of truth for the product name and external links so a rebrand
 * touches one file. Adjust the URLs/email to the real Viato endpoints.
 */
export const BRAND = {
  name: "Viato Voice",
  description: "AI Phone Agents for Viato",
  marketingUrl: "https://viato.ai",
  docsUrl: "https://viato.ai/docs",
  supportEmail: "support@viato.ai",
  privacyUrl: "https://viato.ai/privacy-policy",
  termsUrl: "https://viato.ai/terms-of-service",
} as const;

import "./globals.css";

import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";

import AppLayout from "@/components/layout/AppLayout";
import PostHogIdentify from "@/components/PostHogIdentify";
import { SentryErrorBoundary } from "@/components/SentryErrorBoundary";
import SpinLoader from "@/components/SpinLoader";
import { Toaster } from "@/components/ui/sonner";
import { BRAND } from "@/constants/branding";
import { AppConfigProvider } from "@/context/AppConfigContext";
import { OnboardingProvider } from "@/context/OnboardingContext";
import { TelephonyConfigWarningsProvider } from "@/context/TelephonyConfigWarningsContext";
import { UserConfigProvider } from "@/context/UserConfigContext";
import { AuthProvider } from "@/lib/auth";


const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: BRAND.name,
  description: BRAND.description,
};

export default function RootLayout({
  children
}: {
  children: React.ReactNode
}) {

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Inline script to prevent flash of light theme - runs before React hydrates */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                try {
                  var params = new URLSearchParams(window.location.search);
                  var embedTheme = params.get('embed') === 'clerk' ? params.get('theme') : null;
                  // Embedded "Viato Voice": follow the parent page's theme (passed on the
                  // iframe URL) and remember it across the embed's client-side navigations.
                  // Standalone loads fall back to the user's own saved/system theme.
                  if (embedTheme === 'dark' || embedTheme === 'light') {
                    sessionStorage.setItem('viato_voice_theme', embedTheme);
                  } else {
                    embedTheme = sessionStorage.getItem('viato_voice_theme');
                  }
                  var dark;
                  if (embedTheme === 'dark' || embedTheme === 'light') {
                    dark = embedTheme === 'dark';
                  } else {
                    var theme = localStorage.getItem('theme');
                    dark = theme === 'dark' || (!theme && window.matchMedia('(prefers-color-scheme: dark)').matches);
                  }
                  if (dark) {
                    document.documentElement.classList.add('dark');
                  } else {
                    document.documentElement.classList.remove('dark');
                  }
                } catch (e) {}
              })();
            `,
          }}
        />
      </head>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}>
        <SentryErrorBoundary>
          <AuthProvider>
            <AppConfigProvider>
              <Suspense fallback={<SpinLoader />}>
                <UserConfigProvider>
                  <TelephonyConfigWarningsProvider>
                    <OnboardingProvider>
                      <PostHogIdentify />
                      <AppLayout>
                        {children}
                      </AppLayout>
                      <Toaster />
                    </OnboardingProvider>
                  </TelephonyConfigWarningsProvider>
                </UserConfigProvider>
              </Suspense>
            </AppConfigProvider>
          </AuthProvider>
        </SentryErrorBoundary>
      </body>
    </html>
  );
}

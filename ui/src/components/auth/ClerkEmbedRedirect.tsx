'use client';

import { useRouter } from 'next/navigation';
import { useEffect } from 'react';

import SpinLoader from '@/components/SpinLoader';
import { isEmbedClerkMode } from '@/lib/auth/embedBridge';

/**
 * Root-page handler for the embedded "Viato Voice" deployment (AUTH_PROVIDER=clerk).
 *
 * The root page is a server component and can't authenticate the embed (the token
 * arrives client-side over postMessage). Instead of rendering the sign-in page, we
 * client-redirect to /workflow — which renders client-side under clerk mode and
 * auths via the bridge.
 *
 * Calling isEmbedClerkMode() first persists the embed flag (and parentOrigin) into
 * sessionStorage from the current URL's `?embed=clerk`, so embed mode survives the
 * client-side navigation even though the query string isn't carried along.
 */
export default function ClerkEmbedRedirect() {
  const router = useRouter();

  useEffect(() => {
    // Persist embed mode into sessionStorage before navigating away from the URL
    // that carries `?embed=clerk`.
    isEmbedClerkMode();
    router.replace('/workflow');
  }, [router]);

  return <SpinLoader />;
}

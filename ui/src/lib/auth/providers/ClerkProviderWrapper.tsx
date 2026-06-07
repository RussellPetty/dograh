'use client';

import React, { useEffect, useMemo, useState } from 'react';

import logger from '@/lib/logger';

import {
  decodeClaims,
  getToken,
  initEmbedBridge,
  isEmbedClerkMode,
} from '../embedBridge';
import type { AuthUser, ClerkUser } from '../types';
import { AuthContext } from './AuthProvider';

/**
 * Auth provider for the embedded "Viato Voice" deployment (AUTH_PROVIDER=clerk).
 *
 * The token is supplied by the parent (Viato CRM) over postMessage via the embed
 * bridge — there is no Clerk SDK here and no login page. `getAccessToken` returns
 * the bridge token, which the API client attaches as a Bearer; the backend
 * verifies it against Clerk's JWKS.
 */
export function ClerkProviderWrapper({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<ClerkUser | null>(null);
  const [loading, setLoading] = useState(true);
  // null = undetermined, true/false once the embed mode is known on the client.
  const [embedded, setEmbedded] = useState<boolean | null>(null);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    initEmbedBridge();
    const isEmbedded = isEmbedClerkMode();
    setEmbedded(isEmbedded);

    if (!isEmbedded) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    getToken()
      .then((token) => {
        if (cancelled) return;
        if (token) {
          const claims = decodeClaims(token) || {};
          setUser({
            id: typeof claims.sub === 'string' ? claims.sub : 'embedded',
            email: typeof claims.email === 'string' ? claims.email : undefined,
            organizationId:
              typeof claims.org_id === 'string' ? claims.org_id : undefined,
            provider: 'clerk',
          });
        } else {
          logger.warn('Viato Voice embed: no token received from parent');
        }
        setLoading(false);
      })
      .catch((error) => {
        logger.error('Viato Voice embed: error resolving token', error);
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const getAccessToken = React.useCallback(async () => {
    const token = await getToken();
    return token ?? '';
  }, []);

  // Embedded deployments have no standalone login — the parent owns auth.
  const redirectToLogin = React.useCallback(() => {}, []);
  const logout = React.useCallback(async () => {}, []);

  const contextValue = useMemo(
    () => ({
      user: user as AuthUser,
      isAuthenticated: !!user,
      loading,
      getAccessToken,
      redirectToLogin,
      logout,
      provider: 'clerk' as const,
    }),
    [user, loading, getAccessToken, redirectToLogin, logout],
  );

  // Opened directly (not embedded) under clerk mode: there is no login to show.
  if (!loading && embedded === false) {
    return (
      <div className="flex min-h-screen items-center justify-center p-6 text-center text-sm text-muted-foreground">
        Open Viato Voice from your Viato dashboard.
      </div>
    );
  }

  return <AuthContext.Provider value={contextValue}>{children}</AuthContext.Provider>;
}

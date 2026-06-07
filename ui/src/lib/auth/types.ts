import type { CurrentUser } from '@stackframe/stack';

// Base user interface that all providers must support
export interface BaseUser {
  id: string;
  email?: string;
  name?: string;
  image?: string;
}

// Local/OSS user type
export interface LocalUser extends BaseUser {
  provider: 'local';
  organizationId?: string;
  displayName?: string;
  provider_id?: string;
}

// Clerk user type (embedded "Viato Voice" deployment)
export interface ClerkUser extends BaseUser {
  provider: 'clerk';
  organizationId?: string;
  // Mirror LocalUser's optional fields so existing union accesses
  // (user?.displayName, user?.provider_id) stay type-safe.
  displayName?: string;
  provider_id?: string;
}

// Union type for all user types
export type AuthUser = CurrentUser | LocalUser | ClerkUser;


export interface AuthToken {
  accessToken: string;
  refreshToken?: string;
  expiresAt?: number;
}

export interface TeamPermission {
  id: string;
}

export type AuthProvider = 'stack' | 'local' | 'clerk';

export interface AuthConfig {
  provider: AuthProvider;
  // Provider-specific configuration
  [key: string]: string | number | boolean;
}


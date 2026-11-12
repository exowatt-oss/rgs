import { useQuery } from '@tanstack/react-query'

import { ApiError, fetchApi } from './client'
import type { User } from './types'
import { isMockMode } from '@/lib/mockMode'

const MOCK_ME: User = {
  id: 'mock',
  email: 'alice@example.org',
  name: 'Alice Example',
  avatar_url: null,
  is_admin: false,
  created_at: '2026-10-30T00:00:00Z',
  last_sign_in_at: null,
  actor: null,
  gateway_scope: null,
}

export function useMe() {
  return useQuery({
    queryKey: ['me'],
    queryFn: async (): Promise<User | null> => {
      try {
        return await fetchApi<User>('/auth/me')
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null
        // `?mock=1` (a scripted demo replay) also runs with no backend at
        // all, e.g. `npm run dev` on its own.
        if (isMockMode()) return MOCK_ME
        throw err
      }
    },
    retry: false,
    staleTime: 60_000,
  })
}

/**
 * Redirect to the operator-configured logout URL. Without an external IdP
 * logout setting this only returns to the app; it does not end an IdP session.
 */
export async function logout(): Promise<void> {
  const data = await fetchApi<{ logout_url: string }>('/auth/logout')
  window.location.href = data.logout_url
}

export type { User }

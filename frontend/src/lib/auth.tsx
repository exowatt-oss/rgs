import { createContext, useContext, type ReactNode } from 'react'

import { useMe } from '@/api/hooks'
import type { User } from '@/api/types'

interface AuthContextValue {
  user: User | null
  isLoading: boolean
  isAuthenticated: boolean
  /** True iff `useMe` failed with something OTHER than a clean 401.
   *  Use to distinguish "backend unreachable / 500" from
   *  "user not signed in" — the latter is a normal redirect-to-IdP
   *  trigger, the former is a real error state we should NOT auto-bounce
   *  through the IdP for (the bounce wouldn't fix it). */
  isError: boolean
  error: unknown
  refetch: () => void
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const { data, isLoading, isError, error, refetch } = useMe()

  return (
    <AuthContext.Provider
      value={{
        user: data ?? null,
        isLoading,
        isAuthenticated: Boolean(data),
        isError,
        error,
        refetch,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}

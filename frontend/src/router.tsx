import { type ReactNode } from 'react'
import { Navigate, Outlet, createBrowserRouter } from 'react-router-dom'

import App from '@/App'
import { Wordmark } from '@/components/brand'
import { Skeleton, buttonClass } from '@/components/kit'
import { useAuth } from '@/lib/auth'
import { HomePage } from '@/pages/Home'
import { LivePage } from '@/pages/Live'
import { SessionDetailPage } from '@/pages/SessionDetail'
import { SessionsPage } from '@/pages/Sessions'

// Login is operator-supplied. Never send users to a fixed identity endpoint.
const loginUrl = import.meta.env.VITE_AUTH_LOGIN_URL as string | undefined

function FullScreen({ children }: { children: ReactNode }) {
  return <div className="flex min-h-screen items-center justify-center bg-canvas px-4 text-fg">{children}</div>
}

function StatusCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="w-full max-w-md rounded-[14px] border border-border bg-surface p-6 shadow-pop">
      <Wordmark className="mb-5" />
      <h1 className="text-[15px] font-semibold text-fg">{title}</h1>
      <div className="mt-1.5 flex flex-col items-start gap-4 text-[13px] text-muted">{children}</div>
    </div>
  )
}

/** Only authenticated users may enter the application. */
export function RequireAuth() {
  const { user, isLoading, isError, error, refetch } = useAuth()
  if (isLoading) return <FullScreen><Skeleton className="h-24 w-64 rounded-[10px]" /></FullScreen>
  if (isError) {
    const message = error instanceof Error ? error.message : 'Unknown error'
    return (
      <FullScreen><StatusCard title="Unable to reach backend">
        <p>The app couldn't load your session. <span className="mono">({message})</span></p>
        <button type="button" onClick={() => refetch()} className={buttonClass('primary', { size: 'md' })}>Retry</button>
      </StatusCard></FullScreen>
    )
  }
  if (!user) return (
    <FullScreen><StatusCard title="Sign-in required">
      <p>A verified identity is required. Ask the operator to configure forwarded-auth and an identity provider.</p>
      {loginUrl && <a href={loginUrl} className={buttonClass('primary', { size: 'md' })}>Sign in</a>}
    </StatusCard></FullScreen>
  )
  return <Outlet />
}

function RouteError() {
  return <FullScreen><StatusCard title="Something went wrong"><a className="font-medium underline" href="/">Return to the app</a></StatusCard></FullScreen>
}

export const router = createBrowserRouter([
  {
    element: <RequireAuth />,
    errorElement: <RouteError />,
    children: [
      { element: <App />, children: [
        { path: '/', element: <HomePage /> },
        { path: '/live', element: <LivePage /> },
        { path: '/live/:sessionId', element: <LivePage /> },
        { path: '/sessions', element: <SessionsPage /> },
        { path: '/sessions/:sessionId', element: <SessionDetailPage /> },
        { path: '*', element: <Navigate to="/" replace /> },
      ] },
    ],
  },
])

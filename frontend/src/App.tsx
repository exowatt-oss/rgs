import { useEffect, useRef, useState, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { AudioLines, FlaskConical, History, LogOut, Monitor, Moon, PhoneCall, Sun, X, type LucideIcon } from 'lucide-react'

import { logout } from '@/api/hooks'
import { Wordmark } from '@/components/brand'
import { Avatar, ShellContext } from '@/components/kit'
import { StatusDot } from '@/components/status'
import { useAuth } from '@/lib/auth'
import { useNow } from '@/lib/hooks'
import { formatClock } from '@/lib/live/presentation'
import { LiveRecordProvider, useLiveRecordSelector } from '@/lib/liveRecording'
import { isMockMode } from '@/lib/mockMode'
import { useActiveSessions } from '@/lib/sessions'
import { useTheme, type ThemeChoice } from '@/lib/theme'
import { cn } from '@/lib/utils'

interface NavItem {
  to: string
  label: string
  end: boolean
  icon: LucideIcon
}

const navCls = ({ isActive }: { isActive: boolean }) =>
  cn(
    'group flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] transition-colors duration-100',
    isActive ? 'bg-selected font-medium text-fg' : 'text-fg-2 hover:bg-hover hover:text-fg',
  )

function NavRow({ item, onClose, trailing }: { item: NavItem; onClose?: () => void; trailing?: ReactNode }) {
  return (
    <NavLink to={item.to} end={item.end} onClick={onClose} className={navCls}>
      {({ isActive }) => (
        <>
          <item.icon
            className={cn('h-4 w-4 shrink-0', isActive ? 'text-fg' : 'text-subtle group-hover:text-muted')}
            strokeWidth={1.75}
            aria-hidden
          />
          <span className="min-w-0 flex-1 truncate">{item.label}</span>
          {trailing}
        </>
      )}
    </NavLink>
  )
}

const THEME_NEXT: Record<ThemeChoice, ThemeChoice> = { system: 'light', light: 'dark', dark: 'system' }
const THEME_ICON: Record<ThemeChoice, LucideIcon> = { system: Monitor, light: Sun, dark: Moon }
const THEME_LABEL: Record<ThemeChoice, string> = { system: 'System theme', light: 'Light theme', dark: 'Dark theme' }

export function ThemeButton({ className }: { className?: string }) {
  const { choice, setChoice } = useTheme()
  const ThemeIcon = THEME_ICON[choice]
  return (
    <button
      type="button"
      className={cn(
        'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-hover hover:text-fg',
        className,
      )}
      onClick={() => setChoice(THEME_NEXT[choice])}
      title={`${THEME_LABEL[choice]} — switch to ${THEME_LABEL[THEME_NEXT[choice]].toLowerCase()}`}
      aria-label={`${THEME_LABEL[choice]}; switch theme`}
    >
      <ThemeIcon className="h-4 w-4" aria-hidden />
    </button>
  )
}

function UserFooter() {
  const { user } = useAuth()
  const iconBtn =
    'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-hover hover:text-fg'
  return (
    <div className="border-t border-border">
      {isMockMode() && (
        <div className="flex items-center gap-2 px-3 pt-2.5 text-[12px] text-muted" title="Scripted replay: no microphone, no backend. Add ?mock=0 to leave.">
          <span className="inline-flex h-5 items-center gap-1 rounded-[5px] bg-warn-soft px-1.5 text-[11px] font-medium text-warn-ink">
            <FlaskConical className="h-3 w-3" aria-hidden />
            Demo
          </span>
          Scripted replay
        </div>
      )}
      <div className="flex items-center gap-2.5 px-3 py-2.5">
        {user ? (
          <>
            <Avatar src={user.avatar_url} name={user.name ?? user.email} />
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate text-[12.5px] font-medium text-fg">{user.name ?? user.email.replace(/@.*/, '')}</div>
              <div className="truncate text-[11.5px] text-subtle">{user.email}</div>
            </div>
          </>
        ) : (
          <div className="flex-1" />
        )}
        <ThemeButton />
        {user && (
          <button type="button" onClick={() => void logout()} title="Sign out" className={iconBtn}>
            <LogOut className="h-4 w-4" aria-hidden />
            <span className="sr-only">Sign out</span>
          </button>
        )}
      </div>
    </div>
  )
}

/** Your own running call, one click away from every page. */
function LiveNavRow({ onClose }: { onClose?: () => void }) {
  const phase = useLiveRecordSelector((s) => s.phase)
  const startedAt = useLiveRecordSelector((s) => s.startedAt)
  const sessionId = useLiveRecordSelector((s) => s.session?.id ?? null)
  const active = phase === 'live' || phase === 'starting' || phase === 'stopping' || phase === 'viewing' || phase === 'ended'
  const ticking = phase === 'live' || phase === 'viewing'
  const now = useNow(1000, ticking)
  if (!active) return null
  return (
    <NavRow
      item={{ to: sessionId ? `/live/${sessionId}` : '/live', label: phase === 'viewing' ? 'Following' : phase === 'ended' ? 'Last call' : 'Live call', end: false, icon: AudioLines }}
      onClose={onClose}
      trailing={
        ticking && startedAt ? (
          <span className="flex items-center gap-1.5">
            <StatusDot tone="live" pulse />
            <span className="tabular mono text-[11.5px] text-subtle">{formatClock(now - startedAt)}</span>
          </span>
        ) : null
      }
    />
  )
}

/** Other people's calls in progress — follow along read-only. */
function LiveNowList({ onClose }: { onClose?: () => void }) {
  const { data } = useActiveSessions()
  const mine = useLiveRecordSelector((s) => s.session?.id ?? null)
  const others = (data ?? []).filter((s) => s.id !== mine && s.status === 'live').slice(0, 6)
  if (others.length === 0) return null
  return (
    <div className="mt-5 px-2">
      <div className="flex h-7 items-center px-2 text-[12px] font-medium text-subtle">Live now</div>
      <nav className="space-y-px" aria-label="Live now">
        {others.map((s) => (
          <NavLink key={s.id} to={`/live/${s.id}`} onClick={onClose} className={navCls}>
            <StatusDot tone="live" pulse className="mx-1" />
            <span className="min-w-0 flex-1 truncate">{s.title ?? s.meetCode ?? 'Untitled call'}</span>
            <span className="max-w-[72px] truncate text-[11.5px] text-subtle">{s.startedBy?.name?.split(' ')[0]}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  )
}

export function Sidebar({ onClose }: { onClose?: () => void }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-12 shrink-0 items-center gap-2.5 px-4">
        <NavLink to="/" onClick={onClose} className="flex min-w-0 items-center" aria-label="RGS home">
          <Wordmark />
        </NavLink>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="ml-auto inline-flex h-7 w-7 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-fg"
            aria-label="Close navigation"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        )}
      </div>

      <nav className="mt-1 space-y-px px-2" aria-label="Primary">
        <NavRow item={{ to: '/', label: 'New call', end: true, icon: PhoneCall }} onClose={onClose} />
        <LiveNavRow onClose={onClose} />
        <NavRow item={{ to: '/sessions', label: 'Sessions', end: false, icon: History }} onClose={onClose} />
      </nav>

      <LiveNowList onClose={onClose} />

      <div className="mt-auto">
        <UserFooter />
      </div>
    </div>
  )
}

export default function App() {
  return (
    <LiveRecordProvider>
      <Shell />
    </LiveRecordProvider>
  )
}

function Shell() {
  const [navOpen, setNavOpen] = useState(false)
  const location = useLocation()
  const mainRef = useRef<HTMLElement>(null)

  // New page → top of the scroll area.
  useEffect(() => {
    mainRef.current?.scrollTo?.({ top: 0 })
  }, [location.pathname])

  useEffect(() => {
    if (!navOpen) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setNavOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])

  return (
    <ShellContext.Provider value={{ openNav: () => setNavOpen(true) }}>
      <div className="flex h-dvh w-full overflow-hidden bg-canvas text-fg">
        <aside className="hidden w-[240px] shrink-0 lg:block">
          <Sidebar />
        </aside>

        {navOpen && (
          <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
            <div className="absolute inset-0 bg-overlay" onClick={() => setNavOpen(false)} />
            <div className="drawer-in absolute inset-y-0 left-0 w-[264px] max-w-[85vw] border-r border-border bg-canvas shadow-pop">
              <Sidebar onClose={() => setNavOpen(false)} />
            </div>
          </div>
        )}

        <main
          ref={mainRef}
          id="main"
          className="scroll-quiet relative flex min-w-0 flex-1 flex-col overflow-x-hidden overflow-y-auto bg-panel lg:my-2 lg:mr-2 lg:rounded-[10px] lg:border lg:border-border"
        >
          <Outlet />
        </main>
      </div>
    </ShellContext.Provider>
  )
}

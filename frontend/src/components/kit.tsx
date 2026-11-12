import { createContext, useContext, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Check, ChevronDown, ChevronRight, Copy, Menu, TriangleAlert } from 'lucide-react'

import { formatAbsolute, formatRelative } from '@/lib/format'
import { cn, initials } from '@/lib/utils'

/** Shell → page bridge: lets a page's header open the mobile nav drawer. */
export const ShellContext = createContext<{ openNav: () => void }>({ openNav: () => {} })

export interface Crumb {
  label: ReactNode
  to?: string
}

/**
 * The sticky bar at the top of every page: crumbs (or the page title) on the
 * left, page actions on the right. A short subtitle sits beside the title on
 * wide screens; the bar wraps instead of overflowing on narrow ones.
 */
export function PageHeader({
  title,
  subtitle,
  actions,
  crumbs,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  crumbs?: Crumb[]
}) {
  const { openNav } = useContext(ShellContext)
  return (
    <header className="sticky top-0 z-20 flex min-h-12 flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-panel px-3 py-2 sm:px-5 lg:px-6">
      <div className="flex min-w-0 flex-[1_1_14rem] items-center gap-2">
        <button
          type="button"
          onClick={openNav}
          className="-ml-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-hover hover:text-fg lg:hidden"
          aria-label="Open navigation"
        >
          <Menu className="h-4 w-4" aria-hidden />
        </button>
        {crumbs?.length ? (
          <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1 text-[13px]">
            {crumbs.map((c, i) => (
              <span key={i} className="flex min-w-0 items-center gap-1">
                {i > 0 && <ChevronRight className="h-3.5 w-3.5 shrink-0 text-subtle" aria-hidden />}
                {c.to ? (
                  <Link to={c.to} className="truncate text-muted transition-colors hover:text-fg">
                    {c.label}
                  </Link>
                ) : (
                  <span className="truncate font-medium text-fg">{c.label}</span>
                )}
              </span>
            ))}
          </nav>
        ) : (
          <div className="flex min-w-0 items-baseline gap-3">
            <h1 className="shrink-0 text-[14px] font-semibold tracking-[-0.01em] text-fg">{title}</h1>
            {subtitle && <p className="hidden min-w-0 truncate text-[13px] text-muted md:block">{subtitle}</p>}
          </div>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

/** Page content column under the header. */
export function PageBody({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('mx-auto w-full max-w-[1600px] min-w-0 px-3 py-5 sm:px-5 lg:px-6 lg:py-6', className)}>
      {children}
    </div>
  )
}

export function Panel({
  title,
  aside,
  children,
  className,
  bodyClassName,
  id,
}: {
  title?: ReactNode
  aside?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  id?: string
}) {
  return (
    <section
      id={id}
      className={cn('min-w-0 rounded-[10px] border border-border bg-surface shadow-card', className)}
      aria-label={typeof title === 'string' ? title : undefined}
    >
      {(title || aside) && (
        <header className="flex min-h-11 flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-border px-4 py-2">
          {title && <h2 className="text-[13px] font-semibold text-fg">{title}</h2>}
          {aside && <div className="flex flex-wrap items-center gap-3 text-[12px] text-muted">{aside}</div>}
        </header>
      )}
      <div className={cn('min-w-0', bodyClassName)}>{children}</div>
    </section>
  )
}

/** A single number with a label — the KPI/stat form. */
export function Stat({
  label,
  value,
  hint,
  className,
  tone,
}: {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  className?: string
  /** Text colour for the value when it carries state. */
  tone?: string
}) {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="text-[12px] text-muted">{label}</div>
      <div className={cn('tabular mt-1 text-[22px] leading-7 font-semibold tracking-[-0.02em]', tone ?? 'text-fg')}>
        {value}
      </div>
      {hint && <div className="mt-1 text-[12px] leading-4 text-pretty text-subtle">{hint}</div>}
    </div>
  )
}

export function Time({ iso, now }: { iso?: string | null; now?: number }) {
  if (!iso) return <span className="text-subtle">—</span>
  return (
    <time dateTime={iso} title={formatAbsolute(iso)} className="tabular whitespace-nowrap">
      {formatRelative(iso, now)}
    </time>
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded-md bg-selected', className)} />
}

type ButtonVariant = 'default' | 'primary' | 'ghost' | 'danger' | 'danger-quiet'

export function buttonClass(variant: ButtonVariant = 'default', opts: { active?: boolean; size?: 'sm' | 'md' | 'lg' } = {}) {
  const size = opts.size ?? 'sm'
  return cn(
    'inline-flex items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-[background-color,border-color,color,opacity] duration-100 disabled:pointer-events-none disabled:opacity-50',
    size === 'sm' && 'h-7 px-2.5 text-[12px]',
    size === 'md' && 'h-8 px-3 text-[13px]',
    size === 'lg' && 'h-10 rounded-lg px-5 text-[14px]',
    variant === 'primary'
      ? 'bg-brand text-white hover:bg-brand/90'
      : variant === 'danger'
        ? 'bg-fail text-white hover:bg-fail/90'
        : variant === 'danger-quiet'
          ? 'border border-border bg-surface text-fail-ink shadow-card hover:border-fail/40 hover:bg-fail-soft'
          : variant === 'ghost'
          ? 'text-muted hover:bg-hover hover:text-fg'
          : opts.active
            ? 'border border-border-strong bg-selected text-fg'
            : 'border border-border bg-surface text-fg-2 shadow-card hover:border-border-strong hover:text-fg',
  )
}

export function Button({
  children,
  onClick,
  active,
  className,
  title,
  disabled,
  variant = 'default',
  size = 'sm',
  type = 'button',
  'aria-label': ariaLabel,
  'aria-expanded': ariaExpanded,
}: {
  children: ReactNode
  onClick?: () => void
  active?: boolean
  className?: string
  title?: string
  disabled?: boolean
  variant?: ButtonVariant
  size?: 'sm' | 'md' | 'lg'
  type?: 'button' | 'submit'
  'aria-label'?: string
  'aria-expanded'?: boolean
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      title={title}
      disabled={disabled}
      aria-pressed={active}
      aria-label={ariaLabel}
      aria-expanded={ariaExpanded}
      className={cn(buttonClass(variant, { active, size }), className)}
    >
      {children}
    </button>
  )
}

/** A compact single-choice control. */
export function Segmented<T extends string | number>({
  value,
  options,
  onChange,
  label,
  disabled,
  className,
}: {
  value: T
  options: { value: T; label: ReactNode; title?: string }[]
  onChange: (v: T) => void
  label: string
  disabled?: boolean
  className?: string
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn('inline-flex max-w-full flex-wrap rounded-[7px] bg-selected p-0.5', className)}
    >
      {options.map((o) => {
        const on = o.value === value
        return (
          <button
            key={String(o.value)}
            type="button"
            role="radio"
            aria-checked={on}
            title={o.title}
            disabled={disabled}
            onClick={() => !on && onChange(o.value)}
            className={cn(
              'inline-flex h-6 items-center rounded-[5px] px-2.5 text-[12px] font-medium whitespace-nowrap transition-[background-color,color,box-shadow] duration-100 disabled:opacity-60',
              on ? 'bg-surface text-fg shadow-[0_1px_2px_rgb(0_0_0/0.12)]' : 'text-muted hover:text-fg',
            )}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

/** On/off switch with a visible label beside it (the caller renders the label). */
export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full transition-colors duration-150 disabled:opacity-50',
        checked ? 'bg-brand' : 'bg-track-strong',
      )}
    >
      <span
        className={cn(
          'inline-block h-3.5 w-3.5 rounded-full bg-white shadow-[0_1px_2px_rgb(0_0_0/0.2)] transition-transform duration-150',
          checked ? 'translate-x-[16px]' : 'translate-x-[2px]',
        )}
        aria-hidden
      />
    </button>
  )
}

/** Small neutral label (market, source, kind). */
export function Tag({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex h-5 items-center gap-1 rounded-[5px] border border-border px-1.5 text-[11px] font-medium whitespace-nowrap text-muted',
        className,
      )}
    >
      {children}
    </span>
  )
}

export function Empty({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('px-4 py-10 text-center text-[13px] text-muted', className)}>{children}</div>
}

/** Label/value grid used in detail panels. */
export function Meta({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <dl className={cn('grid grid-cols-[minmax(96px,auto)_minmax(0,1fr)] gap-x-4 gap-y-2 text-[13px]', className)}>
      {children}
    </dl>
  )
}

export function MetaRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-words text-fg">{children}</dd>
    </>
  )
}

/** Error line for a failed mutation/query. */
export function ErrorNote({ error, className }: { error: unknown; className?: string }) {
  if (!error) return null
  const msg = error instanceof Error ? error.message : 'Something went wrong'
  return (
    <p role="alert" className={cn('flex items-start gap-1.5 text-[12.5px] text-fail-ink', className)}>
      <TriangleAlert className="mt-[2px] h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="min-w-0 break-words">{msg}</span>
    </p>
  )
}

/** Round photo with an initials fallback (local directory photos are optional). */
export function Avatar({
  src,
  name,
  size = 28,
  className,
}: {
  src?: string | null
  name?: string | null
  size?: number
  className?: string
}) {
  const [broken, setBroken] = useState(false)
  const style = { width: size, height: size }
  if (src && !broken) {
    return (
      <img
        src={src}
        alt=""
        style={style}
        className={cn('shrink-0 rounded-full bg-selected object-cover', className)}
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
      />
    )
  }
  return (
    <span
      style={{ ...style, fontSize: Math.max(10, Math.round(size * 0.38)) }}
      className={cn('flex shrink-0 items-center justify-center rounded-full bg-selected font-semibold text-fg-2', className)}
      aria-hidden
    >
      {initials(name)}
    </span>
  )
}

export async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // Fallback for non-secure contexts.
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand?.('copy') ?? false
    ta.remove()
    return ok
  }
}

export function CopyButton({
  text,
  label = 'Copy',
  className,
  variant = 'default',
  size = 'sm',
  autoFocus = false,
}: {
  text: string
  label?: string
  className?: string
  variant?: ButtonVariant
  size?: 'sm' | 'md' | 'lg'
  /** Focused when its `Dialog` opens. */
  autoFocus?: boolean
}) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const id = window.setTimeout(() => setCopied(false), 1600)
    return () => window.clearTimeout(id)
  }, [copied])
  return (
    <button
      type="button"
      onClick={async () => setCopied(await writeClipboard(text))}
      className={cn(buttonClass(variant, { size }), 'shrink-0', className)}
      aria-label={copied ? 'Copied' : `${label} to clipboard`}
      data-autofocus={autoFocus || undefined}
    >
      {copied ? (
        <Check className={cn('h-3.5 w-3.5', variant === 'default' && 'text-ok')} aria-hidden />
      ) : (
        <Copy className="h-3.5 w-3.5" aria-hidden />
      )}
      <span aria-live="polite">{copied ? 'Copied' : label}</span>
    </button>
  )
}

/**
 * A command to paste into a terminal. Long commands wrap (break-all) rather
 * than scroll sideways; the copy button always copies the exact string.
 */
export function CommandBlock({ command, className }: { command: string; className?: string }) {
  return (
    <div
      className={cn(
        'flex min-w-0 items-start gap-3 rounded-lg border border-border bg-surface-2 py-2 pr-2 pl-3',
        className,
      )}
    >
      <code className="mono min-w-0 flex-1 pt-[3px] text-[12.5px] leading-5 break-all text-fg">
        <span className="text-subtle select-none" aria-hidden>
          ${' '}
        </span>
        {command}
      </code>
      <CopyButton text={command} />
    </div>
  )
}

/** Inline mono value (IP, instance id) with an inline copy affordance. */
export function MonoCopy({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const id = window.setTimeout(() => setCopied(false), 1400)
    return () => window.clearTimeout(id)
  }, [copied])
  return (
    <button
      type="button"
      onClick={async () => setCopied(await writeClipboard(value))}
      title="Copy"
      className="group inline-flex max-w-full items-center gap-1.5 rounded text-left"
    >
      <span className="mono truncate text-[12.5px]">{value}</span>
      {copied ? (
        <Check className="h-3 w-3 shrink-0 text-ok" aria-label="Copied" />
      ) : (
        <Copy className="h-3 w-3 shrink-0 text-subtle opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden />
      )}
    </button>
  )
}

export const inputClass =
  'h-8 w-full min-w-0 rounded-md border border-border bg-surface px-2.5 text-[13px] text-fg placeholder:text-subtle transition-colors focus:border-border-strong focus:outline-none'

/** Native <select> styled like the inputs (custom chevron, no OS chrome). */
export function SelectField({
  className,
  icon,
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement> & { icon?: ReactNode }) {
  return (
    <span className={cn('relative flex min-w-0 items-center', className)}>
      {icon && <span className="pointer-events-none absolute left-2.5 flex text-subtle">{icon}</span>}
      <select {...props} className={cn(inputClass, 'cursor-pointer appearance-none pr-8', icon && 'pl-8')}>
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2.5 h-3.5 w-3.5 text-subtle" aria-hidden />
    </span>
  )
}

/**
 * Modal on the native <dialog> for content with its own actions (Esc and
 * backdrop close it, like ConfirmDialog). The footer holds a Close button,
 * then `actions`; `aside` sits on the footer's left. An element marked
 * `data-autofocus` takes focus on open. Tall content scrolls vertically.
 */
export function Dialog({
  title,
  icon,
  children,
  actions,
  aside,
  closeLabel = 'Close',
  onClose,
}: {
  title: string
  icon?: ReactNode
  children: ReactNode
  actions?: ReactNode
  aside?: ReactNode
  closeLabel?: string
  onClose: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const el = ref.current
    if (el && !el.open) el.showModal?.()
    el?.querySelector<HTMLElement>('[data-autofocus]')?.focus()
  }, [])
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && ref.current?.close()}
      aria-labelledby={titleId}
      className="pop-in m-auto max-h-[calc(100dvh-2rem)] w-[min(36rem,calc(100vw-2rem))] overflow-x-hidden overflow-y-auto rounded-[14px] border border-border bg-surface p-0 text-fg shadow-pop backdrop:bg-overlay"
    >
      <div className="flex flex-col gap-4 p-5">
        <div className="flex items-start gap-3">
          {icon && (
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-selected text-fg-2">
              {icon}
            </span>
          )}
          <h2 id={titleId} className={cn('min-w-0 text-[15px] font-semibold', !!icon && 'pt-[7px]')}>
            {title}
          </h2>
        </div>
        <div className="min-w-0 text-[13px] text-pretty text-muted">{children}</div>
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          {aside && <div className="min-w-0 text-[12.5px]">{aside}</div>}
          <div className="ml-auto flex flex-wrap justify-end gap-2">
            <Button onClick={() => ref.current?.close()}>{closeLabel}</Button>
            {actions}
          </div>
        </div>
      </div>
    </dialog>
  )
}

/**
 * Modal confirm on the native <dialog> (Esc and backdrop close it). With
 * `confirmText`, the user must type that exact string to enable the action.
 */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  confirmText,
  danger = false,
  pending = false,
  error,
  onConfirm,
  onClose,
}: {
  title: string
  body: ReactNode
  confirmLabel: string
  confirmText?: string
  danger?: boolean
  pending?: boolean
  error?: unknown
  onConfirm: () => void
  onClose: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const [typed, setTyped] = useState('')
  useEffect(() => {
    const el = ref.current
    if (el && !el.open) el.showModal?.()
  }, [])
  const ok = !pending && (!confirmText || typed.trim() === confirmText)
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && ref.current?.close()}
      aria-labelledby="confirm-title"
      className="pop-in m-auto w-[min(30rem,calc(100vw-2rem))] rounded-[14px] border border-border bg-surface p-0 text-fg shadow-pop backdrop:bg-overlay"
    >
      <form
        method="dialog"
        className="flex flex-col gap-4 p-5"
        onSubmit={(e) => {
          e.preventDefault()
          if (ok) onConfirm()
        }}
      >
        <div className="flex items-start gap-3">
          <span
            className={cn(
              'flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px]',
              danger ? 'bg-fail-soft' : 'bg-warn-soft',
            )}
          >
            <TriangleAlert className={cn('h-[18px] w-[18px]', danger ? 'text-fail-ink' : 'text-warn-ink')} aria-hidden />
          </span>
          <div className="min-w-0">
            <h2 id="confirm-title" className="text-[15px] font-semibold">
              {title}
            </h2>
            <div className="mt-1 text-[13px] text-pretty text-muted">{body}</div>
          </div>
        </div>
        {confirmText && (
          <label className="flex flex-col gap-1.5 text-[13px]">
            <span>
              Type <b className="mono font-medium text-fg">{confirmText}</b> to confirm
            </span>
            <input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              className={cn(inputClass, 'mono')}
              aria-label={`Type ${confirmText} to confirm`}
            />
          </label>
        )}
        <ErrorNote error={error} />
        <div className="flex flex-wrap justify-end gap-2">
          <Button onClick={() => ref.current?.close()}>Cancel</Button>
          <Button type="submit" variant={danger ? 'danger' : 'primary'} disabled={!ok}>
            {pending ? 'Working…' : confirmLabel}
          </Button>
        </div>
      </form>
    </dialog>
  )
}

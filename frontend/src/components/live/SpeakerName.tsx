import * as Popover from '@radix-ui/react-popover'
import * as Tooltip from '@radix-ui/react-tooltip'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { AudioLines, Check, IdCard, UserRound, Users } from 'lucide-react'
import { createContext, useContext, useId, useMemo, useState, type ReactNode } from 'react'

import { inputClass } from '@/components/kit'
import { PersonIdentity } from '@/components/live/PersonIdentity'
import { isTentativeName, type NameSuggestion } from '@/lib/live/conversation'
import { isPlaceholderName, speakerInitials, speakerName, type SpeakerEntry } from '@/lib/live/liveReducer'
import { speakerClasses } from '@/lib/live/presentation'
import { cn } from '@/lib/utils'

const EASE = [0.22, 1, 0.36, 1] as const

/**
 * Renaming, for whoever renders a speaker name: whether this viewer may, how,
 * and which names to offer. Absent (or `rename: null`) means read-only.
 */
export interface SpeakerRenaming {
  rename: ((speakerId: string, name: string, email: string | null) => void) | null
  suggestionsFor: (speaker: SpeakerEntry) => NameSuggestion[]
}

const RenameContext = createContext<SpeakerRenaming | null>(null)

export function SpeakerRenameProvider({ value, children }: { value: SpeakerRenaming | null; children: ReactNode }) {
  return <RenameContext.Provider value={value}>{children}</RenameContext.Provider>
}

/** Speaker avatar: a filled disc in the speaker's colour with initials once
 *  named (else the speaker number). The text crossfades when a name arrives;
 *  a ring breathes while they hold the floor. */
export function SpeakerAvatar({
  speaker,
  size = 28,
  speaking = false,
  className,
}: {
  speaker: SpeakerEntry | undefined
  size?: number
  speaking?: boolean
  className?: string
}) {
  const cls = speakerClasses(speaker?.colorIndex)
  const text = speakerInitials(speaker)
  const reduce = useReducedMotion()
  return (
    <span aria-hidden className={cn('relative inline-grid shrink-0 place-items-center', className)} style={{ width: size, height: size }}>
      {speaking && (
        <span
          className={cn('absolute rounded-full', size < 20 ? '-inset-[2px] ring-[1.5px]' : '-inset-[3px] ring-2', cls.ring, !reduce && 'speaking-ring')}
        />
      )}
      <span
        className={cn('relative inline-grid size-full place-items-center overflow-hidden rounded-full font-semibold', cls.avatar)}
        style={{ fontSize: Math.max(9, size * 0.38) }}
      >
        <AnimatePresence initial={false} mode="popLayout">
          <motion.span
            key={text}
            initial={reduce ? { opacity: 0 } : { opacity: 0, y: size * 0.5 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: -size * 0.5 }}
            transition={{ duration: 0.45, ease: EASE }}
            className="tabular leading-none"
          >
            {text}
          </motion.span>
        </AnimatePresence>
      </span>
    </span>
  )
}

/**
 * A speaker's name. Placeholders ("Speaker 3") read quieter; a guessed name
 * reads "Alice?" until someone confirms it. When a self-introduction names a
 * speaker the new name rolls in under a brief gradient sweep. With renaming
 * available the name is the rename control.
 */
export function SpeakerName({
  speaker,
  showSource = false,
  className,
}: {
  speaker: SpeakerEntry | undefined
  /** Also show where the name came from ("named from intro"). */
  showSource?: boolean
  className?: string
}) {
  const renaming = useContext(RenameContext)
  const name = speakerName(speaker)
  const reduce = useReducedMotion()
  const placeholder = isPlaceholderName(speaker)
  const tentative = isTentativeName(speaker)
  const fromIntro = speaker?.nameSource === 'self_intro' && !placeholder
  const canRename = !!renaming?.rename && !!speaker

  const label = (
    <span className="relative inline-flex min-w-0 overflow-hidden">
      <AnimatePresence initial={false} mode="wait">
        <motion.span
          key={name}
          initial={reduce ? { opacity: 0 } : { opacity: 0, y: '80%' }}
          animate={{ opacity: 1, y: '0%' }}
          exit={reduce ? { opacity: 0 } : { opacity: 0, y: '-80%' }}
          transition={{ duration: 0.3, ease: EASE }}
          className={cn(
            'relative truncate text-[13px] font-semibold',
            placeholder ? 'text-fg-2' : 'text-fg',
            tentative && 'underline decoration-border-strong decoration-dotted underline-offset-[3px]',
            className,
          )}
        >
          {name}
          {tentative && (
            <span className="font-normal text-muted" aria-label=" (unconfirmed)">
              ?
            </span>
          )}
          {!reduce && fromIntro && (
            <motion.span
              aria-hidden
              initial={{ scaleX: 0, opacity: 1 }}
              animate={{ scaleX: 1, opacity: 0 }}
              transition={{ duration: 1.4, ease: EASE, opacity: { delay: 0.9, duration: 0.6 } }}
              className="ex-bg-gradient absolute -bottom-0.5 left-0 h-px w-full origin-left"
            />
          )}
        </motion.span>
      </AnimatePresence>
    </span>
  )

  const control =
    canRename && speaker && renaming?.rename ? (
      <RenamePopover speaker={speaker} rename={renaming.rename} suggestions={renaming.suggestionsFor(speaker)}>
        {label}
      </RenamePopover>
    ) : tentative && speaker ? (
      <GuessTooltip speaker={speaker}>{label}</GuessTooltip>
    ) : speaker?.person ? (
      <IdentityTooltip speaker={speaker}>{label}</IdentityTooltip>
    ) : (
      label
    )

  return (
    <span className="inline-flex min-w-0 items-center gap-2">
      {control}
      {showSource && fromIntro && speaker && !tentative && <IntroBadge speaker={speaker} />}
    </span>
  )
}

function confidenceText(speaker: SpeakerEntry): string | null {
  return speaker.nameConfidence != null ? `${Math.round(speaker.nameConfidence * 100)}% sure` : null
}

function GuessTooltip({ speaker, children }: { speaker: SpeakerEntry; children: ReactNode }) {
  return (
    <Tooltip.Provider delayDuration={150}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <span tabIndex={0} className="inline-flex min-w-0 cursor-default rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-brand/50">
            {children}
          </span>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content side="top" sideOffset={6} className="z-50 max-w-72 rounded-lg border border-border bg-surface px-3 py-2 text-[12px] leading-relaxed text-fg shadow-pop">
            A guess{speaker.nameEvidence ? <> from “{speaker.nameEvidence}”</> : null}
            {confidenceText(speaker) && <span className="ml-1 mono text-[10.5px] text-muted">· {confidenceText(speaker)}</span>}
            <Tooltip.Arrow className="fill-surface" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  )
}

/** A recognised speaker's name: hover/focus says who they are (Slack, email). */
function IdentityTooltip({ speaker, children }: { speaker: SpeakerEntry; children: ReactNode }) {
  return (
    <Tooltip.Provider delayDuration={150}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <span tabIndex={0} className="inline-flex min-w-0 cursor-default rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-brand/50">
            {children}
          </span>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content side="top" sideOffset={6} className="z-50 max-w-80 rounded-lg border border-border bg-surface px-3 py-2 text-[12px] leading-relaxed text-fg shadow-pop">
            <PersonIdentity person={speaker.person} name={speakerName(speaker)} />
            <Tooltip.Arrow className="fill-surface" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  )
}

function IntroBadge({ speaker }: { speaker: SpeakerEntry }) {
  return (
    <Tooltip.Provider delayDuration={150}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <span
            tabIndex={0}
            className="inline-flex h-[18px] shrink-0 cursor-default items-center gap-1 rounded-[5px] bg-selected px-1.5 text-[11px] text-muted outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
          >
            <IdCard className="size-3" aria-hidden />
            from intro
          </span>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            side="top"
            sideOffset={6}
            className="z-50 max-w-72 rounded-lg border border-border bg-surface px-3 py-2 text-[12px] leading-relaxed text-fg shadow-pop"
          >
            {speaker.nameEvidence ? <>Heard “{speaker.nameEvidence}”</> : 'Named from a self-introduction'}
            {confidenceText(speaker) && <span className="ml-1 mono text-[10.5px] text-muted">· {confidenceText(speaker)}</span>}
            <Tooltip.Arrow className="fill-surface" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  )
}

const REASON: Record<NameSuggestion['reason'], { label: (s: NameSuggestion) => string; icon: typeof Users }> = {
  voice: { label: (s) => (s.score != null ? `voice match · ${Math.round(s.score * 100)}%` : 'voice match'), icon: AudioLines },
  guess: { label: () => 'heard in the call', icon: IdCard },
  attendee: { label: () => 'invited', icon: Users },
  owner: { label: () => 'started this call', icon: UserRound },
}

/** Click a name → pick a suggestion or type one. Arrow keys move through the
 *  list, Enter saves, Esc closes. */
function RenamePopover({
  speaker,
  rename,
  suggestions,
  children,
}: {
  speaker: SpeakerEntry
  rename: (speakerId: string, name: string, email: string | null) => void
  suggestions: NameSuggestion[]
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState('')
  const [active, setActive] = useState(0)
  const listId = useId()
  const inputId = useId()
  const typed = value.trim()
  const matches = useMemo(() => {
    const q = typed.toLowerCase()
    const list = q ? suggestions.filter((s) => s.name.toLowerCase().includes(q)) : suggestions
    return list.slice(0, 6)
  }, [suggestions, typed])
  const exact = matches.some((m) => m.name.toLowerCase() === typed.toLowerCase())
  // Options: the suggestions, then "Use “typed”" when it's new.
  const options: (NameSuggestion | { name: string; email: null; reason: 'typed'; score: null })[] = [
    ...matches,
    ...(typed && !exact ? [{ name: typed, email: null, reason: 'typed' as const, score: null }] : []),
  ]
  const current = isPlaceholderName(speaker) ? null : speakerName(speaker)

  const save = (name: string, email: string | null) => {
    const clean = name.trim()
    if (clean) rename(speaker.id, clean, email)
    setOpen(false)
  }

  return (
    <Popover.Root
      open={open}
      onOpenChange={(o) => {
        setOpen(o)
        setValue('')
        setActive(0)
      }}
    >
      <Popover.Trigger
        className="group/rename relative inline-flex min-w-0 items-center rounded-md outline-none decoration-border-strong decoration-dotted underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-brand/50"
        aria-label={`Rename ${speakerName(speaker)}`}
        title="Rename speaker"
      >
        {children}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="bottom"
          align="start"
          sideOffset={6}
          collisionPadding={12}
          className="pop-in z-50 w-[min(18rem,calc(100vw-24px))] rounded-[10px] border border-border bg-surface p-1.5 shadow-pop"
        >
          <form
            onSubmit={(e) => {
              e.preventDefault()
              const pick = options[active] ?? (typed ? { name: typed, email: null } : null)
              if (pick) save(pick.name, pick.email)
            }}
          >
            <label htmlFor={inputId} className="flex items-center gap-2 px-2 pt-1.5 pb-1 text-[12px] text-muted">
              <SpeakerAvatar speaker={speaker} size={18} />
              <span className="min-w-0 truncate">
                {current ? (
                  <>
                    Who is <span className="font-medium text-fg-2">{current}</span>, really?
                  </>
                ) : (
                  'Who is this?'
                )}
              </span>
            </label>
            {speaker.person && <PersonIdentity person={speaker.person} className="flex px-2 pb-1.5" />}
            <input
              id={inputId}
              autoFocus
              autoComplete="off"
              role="combobox"
              aria-expanded={options.length > 0}
              aria-controls={listId}
              aria-activedescendant={options.length ? `${listId}-${active}` : undefined}
              value={value}
              onChange={(e) => {
                setValue(e.target.value)
                setActive(0)
              }}
              onKeyDown={(e) => {
                if (e.key === 'ArrowDown') {
                  e.preventDefault()
                  setActive((a) => Math.min(options.length - 1, a + 1))
                } else if (e.key === 'ArrowUp') {
                  e.preventDefault()
                  setActive((a) => Math.max(0, a - 1))
                }
              }}
              placeholder="Type a name"
              className={cn(inputClass, 'h-8')}
            />
            {options.length > 0 ? (
              <ul id={listId} role="listbox" aria-label="Suggested names" className="mt-1 flex flex-col">
                {options.map((o, i) => {
                  const meta = o.reason === 'typed' ? null : REASON[o.reason]
                  const Icon = meta?.icon ?? Check
                  return (
                    <li
                      key={`${o.reason}:${o.name}`}
                      id={`${listId}-${i}`}
                      role="option"
                      aria-selected={i === active}
                      onMouseEnter={() => setActive(i)}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => save(o.name, o.email)}
                      className={cn(
                        'flex min-h-9 cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 text-[13px]',
                        i === active ? 'bg-selected text-fg' : 'text-fg-2',
                      )}
                    >
                      <Icon className="size-3.5 shrink-0 text-subtle" aria-hidden />
                      <span className="min-w-0 flex-1 truncate">
                        {o.reason === 'typed' ? (
                          <>
                            Use <span className="font-medium text-fg">“{o.name}”</span>
                          </>
                        ) : (
                          <span className="font-medium">{o.name}</span>
                        )}
                      </span>
                      {meta && <span className="shrink-0 text-[11px] text-subtle">{meta.label(o as NameSuggestion)}</span>}
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="px-2 pt-2 pb-1 text-[12px] text-subtle">Type their name and press Enter.</p>
            )}
          </form>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

import { Fragment, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { cn } from '@/lib/utils'

// ---------------------------------------------------------------------------
// Minimal, safe markdown for agent finding bodies: paragraphs, bullet and
// numbered lists, **bold**, *italic*, `code`, [links](...). Renders React
// elements only (no HTML injection). In-app paths become router links.
// ---------------------------------------------------------------------------

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\)|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let i = 0
  for (const match of text.matchAll(INLINE)) {
    const token = match[0]
    const at = match.index ?? 0
    if (at > last) out.push(text.slice(last, at))
    const key = `${keyPrefix}-${i++}`
    if (token.startsWith('**')) out.push(<strong key={key} className="font-semibold text-fg">{token.slice(2, -2)}</strong>)
    else if (token.startsWith('`')) out.push(<code key={key} className="rounded bg-selected px-1 py-0.5 mono text-[0.85em]">{token.slice(1, -1)}</code>)
    else if (token.startsWith('[')) {
      const m = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token)!
      out.push(<MarkdownLink key={key} href={m[2]}>{m[1]}</MarkdownLink>)
    } else out.push(<em key={key}>{token.slice(1, -1)}</em>)
    last = at + token.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function MarkdownLink({ href, children }: { href: string; children: ReactNode }) {
  const cls = 'font-medium text-fg underline decoration-brand/60 underline-offset-2 hover:decoration-brand'
  if (href.startsWith('/') && !href.startsWith('//')) return <Link to={href} className={cls}>{children}</Link>
  if (/^https?:\/\//i.test(href)) return <a href={href} target="_blank" rel="noreferrer" className={cls}>{children}</a>
  return <>{children}</>
}

export function MiniMarkdown({ source, className }: { source: string; className?: string }) {
  const blocks: ReactNode[] = []
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  let para: string[] = []
  let list: { ordered: boolean; items: string[] } | null = null
  const flushPara = () => {
    if (para.length) blocks.push(<p key={`p${blocks.length}`}>{renderInline(para.join(' '), `p${blocks.length}`)}</p>)
    para = []
  }
  const flushList = () => {
    if (!list) return
    const k = `l${blocks.length}`
    const items = list.items.map((item, j) => <li key={j}>{renderInline(item, `${k}-${j}`)}</li>)
    blocks.push(list.ordered
      ? <ol key={k} className="list-decimal space-y-0.5 pl-5 marker:font-mono marker:text-subtle">{items}</ol>
      : <ul key={k} className="list-disc space-y-0.5 pl-5 marker:text-subtle">{items}</ul>)
    list = null
  }
  for (const raw of lines) {
    const line = raw.trim()
    const bullet = /^[-*•]\s+(.*)$/.exec(line)
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line)
    if (!line) {
      flushPara()
      flushList()
    } else if (bullet || numbered) {
      flushPara()
      const ordered = !!numbered
      if (list && list.ordered !== ordered) flushList()
      if (!list) list = { ordered, items: [] }
      list.items.push((bullet ?? numbered)![1])
    } else {
      flushList()
      para.push(line.replace(/^#{1,6}\s+/, ''))
    }
  }
  flushPara()
  flushList()
  return <div className={cn('flex flex-col gap-1.5', className)}>{blocks.map((b, i) => <Fragment key={i}>{b}</Fragment>)}</div>
}

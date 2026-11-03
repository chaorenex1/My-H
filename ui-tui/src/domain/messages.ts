import type { SessionInfo } from '@hermes/shared/gateway-types'

import { LONG_MSG } from '../config/limits.js'
import { t } from '../i18n/runtime.js'
import { buildToolTrailLine } from '../lib/text.js'
import type { AdviserMeta, Msg } from '../types.js'

// Adviser rows are persisted as rendered text: a bracketed header, one `- [label] text`
// line per advice, and a footer. The card shows the same body the Desktop parses.
const ADVISER_HEADER_RE = /^\[adviser[^\n]*\]\s*/
const ADVISER_FOOTER_RE = /\s*\[end adviser\]\s*$/
const ADVISER_ITEM_RE = /^- \[([^\]]+)\]\s*(.*)$/gm
const ADVISER_SEVERITY_RANK: Record<string, number> = { blocker: 3, concern: 2, nit: 1 }

/** Split an adviser block into its display body and card metadata. */
export const adviserFromText = (content: string): { body: string; meta: AdviserMeta } | undefined => {
  const raw = String(content || '').trim()

  if (!raw) {
    return undefined
  }

  const body = raw.replace(ADVISER_HEADER_RE, '').replace(ADVISER_FOOTER_RE, '').trim()
  const items = [...body.matchAll(ADVISER_ITEM_RE)].map(match => {
    const label = match[1].trim()

    return { label, severity: label.split(' · ')[0]?.trim() || 'advisory', text: match[2].trim() }
  })
  // A mixed block is as loud as its strongest row, not as its first one.
  const top = items.reduce<(typeof items)[number] | undefined>(
    (best, item) =>
      !best || (ADVISER_SEVERITY_RANK[item.severity] ?? 0) > (ADVISER_SEVERITY_RANK[best.severity] ?? 0) ? item : best,
    undefined
  )

  return {
    body,
    meta: { items, label: top?.label ?? 'advisory', severity: top?.severity ?? 'advisory' }
  }
}

/** The adviser card metadata from the gateway's `details.notes` (ADR-10 D1). */
export const adviserMetaFromNotes = (notes: readonly AdviserNote[]): AdviserMeta => {
  const items = notes.map(note => {
    const severity = note.severity ?? 'advisory'
    const label = [severity, note.advisor].filter(Boolean).join(' · ')

    return { label, severity, text: note.note ?? '' }
  })
  const top = items.reduce<(typeof items)[number] | undefined>(
    (best, item) =>
      !best || (ADVISER_SEVERITY_RANK[item.severity] ?? 0) > (ADVISER_SEVERITY_RANK[best.severity] ?? 0) ? item : best,
    undefined
  )

  return { items, label: top?.label ?? 'advisory', severity: top?.severity ?? 'advisory' }
}

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

export const userDisplay = (text: string) => {
  if (text.length <= LONG_MSG) {
    return text
  }

  const first = text.split('\n')[0]?.trim() ?? ''
  const words = first.split(/\s+/).filter(Boolean)
  const prefix = (words.length > 1 ? words.slice(0, 4).join(' ') : first).slice(0, 80)

  return t('libText.messages.longMessage', prefix || t('libText.messages.messageFallback'))
}

/** A resumed user row's durable message row (backtrack browsing finds its prompt by it). */
const storedRowId = (role: string, rowId: unknown): Pick<Msg, 'rowId'> =>
  role === 'user' && Number.isSafeInteger(rowId) && (rowId as number) > 0 ? { rowId: rowId as number } : {}

export const toTranscriptMessages = (rows: unknown): Msg[] => {
  if (!Array.isArray(rows)) {
    return []
  }

  const out: Msg[] = []
  let pending: string[] = []

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const { context, custom_type, details, name, role, row_id, synthetic_kind, text, timestamp } =
      row as TranscriptRow

    const createdAt =
      typeof timestamp === 'number' && Number.isFinite(timestamp) && timestamp > 0 ? timestamp : undefined

    if (role === 'tool') {
      pending.push(buildToolTrailLine(name ?? 'tool', context ?? ''))

      continue
    }

    if (typeof text !== 'string' || !text.trim()) {
      continue
    }

    // ADR-10 P5: custom blocks and the one synthetic row marker replace the retired
    // display_kind. Timeline notices render as dim ◈ markers, not opaque user messages.
    if (role === 'custom') {
      const event = customEventText(custom_type, details)
      if (event !== undefined) {
        out.push({ kind: 'event', role: 'system', text: event })
        pending = []

        continue
      }
      if (custom_type === 'advisor') {
        // Plugin guidance the model saw; never a human prompt. Details are canonical (D1);
        // the text parse only recovers the body for an older gateway.
        const parsed = adviserFromText(text)
        const notes = Array.isArray(details?.notes) ? details.notes : undefined
        const meta = notes ? adviserMetaFromNotes(notes) : parsed?.meta
        out.push({
          ...(meta ? { adviser: meta } : {}),
          role: 'system',
          text: parsed?.body ?? text,
          ...(createdAt !== undefined && { createdAt })
        })
        pending = []

        continue
      }
      // skill-prompt / todo-reminder / anything custom without a dedicated line: its text.
      out.push({ role: 'user', text, ...(createdAt !== undefined && { createdAt }) })
      pending = []

      continue
    }

    if (synthetic_kind === 'auto_continue') {
      out.push({ kind: 'event', role: 'system', text: t('libText.messages.resumedInterruptedTurn') })
      pending = []

      continue
    }

    if (role === 'assistant') {
      out.push({ role, text, ...(createdAt !== undefined && { createdAt }), ...(pending.length && { tools: pending }) })
      pending = []
    } else if (role === 'user' || role === 'system') {
      out.push({ role, text, ...(createdAt !== undefined && { createdAt }), ...storedRowId(role, row_id) })
      pending = []
    }
  }

  return out
}

/** One custom block's dim timeline line, or ``undefined`` when it renders as a message instead. */
const customEventText = (customType: unknown, details: TranscriptRow['details']): string | undefined => {
  const displayText = details && typeof details.display_text === 'string' ? details.display_text : undefined
  const count = details && typeof details.task_count === 'number' ? details.task_count : undefined

  if (customType === 'model-switch') {
    return t('libText.messages.modelChanged')
  }
  if (customType === 'personality-switch') {
    return t('libText.messages.personalityChanged')
  }
  if (customType === 'process-complete') {
    return displayText ?? t('libText.messages.backgroundProcessFinished')
  }
  if (customType === 'async-delegation-complete') {
    return (
      displayText ??
      (count === undefined
        ? t('libText.messages.backgroundAgentWorkFinished')
        : t(
            count === 1
              ? 'libText.messages.backgroundAgentsFinishedOne'
              : 'libText.messages.backgroundAgentsFinishedOther',
            count
          ))
    )
  }
  if (customType === 'internal-notification') {
    return displayText
  }
  return undefined
}

export const fmtDuration = (ms: number) => {
  const t = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60

  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`
}

interface AdviserNote {
  advisor?: string
  note?: string
  severity?: string
}

interface TranscriptRow {
  context?: string
  custom_type?: string
  details?: {
    display_text?: string
    notes?: AdviserNote[]
    task_count?: number
    [key: string]: unknown
  }
  name?: string
  role?: string
  row_id?: number
  synthetic_kind?: string
  text?: string
  timestamp?: number
}

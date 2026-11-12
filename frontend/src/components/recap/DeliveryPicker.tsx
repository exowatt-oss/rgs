import { Mail, MessageSquare } from 'lucide-react'

import type { LiveRecapConfig } from '@/api/live'
import { Segmented } from '@/components/kit'

import type { DeliveryChoice } from './recap'

/** Slack / Email / Both: where the edition link goes when the session stops. */
export function DeliveryPicker({
  value,
  onChange,
  config,
  disabled,
}: {
  value: DeliveryChoice
  onChange: (v: DeliveryChoice) => void
  config: LiveRecapConfig | null | undefined
  disabled?: boolean
}) {
  const available = (id: 'slack' | 'email') => config?.channels.find((c) => c.id === id)?.available ?? true
  const off = (ids: ('slack' | 'email')[]) => ids.some((id) => !available(id))
  return (
    <Segmented
      label="Send the edition link"
      value={value}
      onChange={onChange}
      disabled={disabled}
      options={[
        {
          value: 'slack',
          title: off(['slack']) ? 'Slack isn’t connected on this server yet' : 'A Slack DM from RGS',
          label: (
            <span className="inline-flex items-center gap-1.5">
              <MessageSquare className="h-3.5 w-3.5" aria-hidden />
              Slack
            </span>
          ),
        },
        {
          value: 'email',
          title: off(['email']) ? 'Email isn’t configured on this server yet' : 'An email to your example address',
          label: (
            <span className="inline-flex items-center gap-1.5">
              <Mail className="h-3.5 w-3.5" aria-hidden />
              Email
            </span>
          ),
        },
        { value: 'both', title: 'Slack and email', label: 'Both' },
      ]}
    />
  )
}

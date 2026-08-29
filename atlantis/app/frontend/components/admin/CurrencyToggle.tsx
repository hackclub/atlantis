import { Fish, Coins } from 'lucide-react'
import { cn } from '@/lib/utils'

export type Currency = 'koi' | 'gold'

const OPTIONS: { value: Currency; label: string; icon: typeof Fish }[] = [
  { value: 'koi', label: 'Koi', icon: Fish },
  { value: 'gold', label: 'Gold', icon: Coins },
]

// Segmented control for switching the active currency. Shared by the ledger index and the
// adjustment form so the two stay visually identical.
export default function CurrencyToggle({
  value,
  onChange,
  size = 'default',
}: {
  value: Currency
  onChange: (currency: Currency) => void
  size?: 'sm' | 'default'
}) {
  return (
    <div className="inline-flex rounded-md border border-input bg-background p-0.5" role="tablist">
      {OPTIONS.map(({ value: v, label, icon: Icon }) => (
        <button
          key={v}
          type="button"
          role="tab"
          aria-selected={value === v}
          onClick={() => onChange(v)}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-[5px] font-medium transition-colors',
            size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-sm',
            value === v
              ? 'bg-primary text-primary-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          <Icon className={size === 'sm' ? 'size-3.5' : 'size-4'} />
          {label}
        </button>
      ))}
    </div>
  )
}

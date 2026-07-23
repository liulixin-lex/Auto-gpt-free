import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center gap-1 border px-2 py-0.5 font-[family-name:var(--font-mono)] text-[10px] font-bold uppercase tracking-[0.06em]',
  {
    variants: {
      variant: {
        default: 'border-[var(--accent-edge)] bg-[var(--accent-soft)] text-[var(--accent-strong)]',
        success: 'border-[color:oklch(0.76_0.14_145_/_0.35)] bg-[var(--ok-soft)] text-[var(--ok)]',
        warning: 'border-[color:oklch(0.8_0.14_85_/_0.35)] bg-[var(--warn-soft)] text-[var(--warn)]',
        danger: 'border-[color:oklch(0.65_0.18_25_/_0.35)] bg-[var(--danger-soft)] text-[var(--danger)]',
        secondary: 'border-[var(--border)] bg-[var(--chip-bg)] text-[var(--text-muted)]',
      },
    },
    defaultVariants: { variant: 'default' },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }

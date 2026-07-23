import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center rounded-none text-[13px] font-semibold tracking-tight transition-colors duration-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-base)] disabled:pointer-events-none disabled:opacity-45 active:translate-x-[1px] active:translate-y-[1px]',
  {
    variants: {
      variant: {
        default:
          'border-2 border-[var(--accent)] bg-[var(--accent)] text-[#04140f] shadow-[2px_2px_0_rgba(0,0,0,0.45)] hover:bg-[var(--accent-hover)]',
        destructive:
          'border-2 border-[var(--danger)] bg-[var(--danger)] text-white shadow-[2px_2px_0_rgba(0,0,0,0.45)] hover:opacity-90',
        outline:
          'border-2 border-[var(--border-hard)] bg-[var(--bg-pane)] text-[var(--text-secondary)] shadow-[2px_2px_0_rgba(0,0,0,0.35)] hover:border-[var(--accent-edge)] hover:bg-[var(--accent-soft)] hover:text-[var(--text-primary)]',
        ghost:
          'border-2 border-transparent text-[var(--text-secondary)] hover:border-[var(--border)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]',
        link: 'border-transparent underline-offset-4 hover:underline text-[var(--text-accent)]',
      },
      size: {
        default: 'h-9 px-3.5',
        sm: 'h-8 px-2.5 text-[12px]',
        lg: 'h-10 px-4',
        icon: 'h-9 w-9',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
      <Comp
        className={cn(buttonVariants({ variant, size }), className)}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = 'Button'

export { Button, buttonVariants }

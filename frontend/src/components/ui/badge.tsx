/** shadcn badge, owned source.
 *
 * The status variants are the only place a state colour is chosen in this app, which
 * is what keeps "OPEN_UNPROTECTED is danger" a single decision rather than one made
 * again in every table that renders a state.
 */

import { cva, type VariantProps } from "class-variance-authority"
import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "border-border bg-muted text-foreground",
        muted: "border-transparent bg-muted text-muted-foreground",
        outline: "border-border bg-transparent text-muted-foreground",
        primary: "border-transparent bg-primary-soft text-primary",
        success: "border-success-border bg-success-soft text-success",
        danger: "border-danger-border bg-danger-soft text-danger",
        warn: "border-warn-border bg-warn-soft text-warn",
        info: "border-info-border bg-info-soft text-info"
      }
    },
    defaultVariants: { variant: "default" }
  }
)

export type BadgeProps = ComponentProps<"span"> & VariantProps<typeof badgeVariants>

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />
}

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>

export { badgeVariants }

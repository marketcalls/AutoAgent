/** shadcn button, owned source.
 *
 * Two variants exist that shadcn does not ship: `success` and `halt`. They are here
 * because this app's two irreversible actions - approving a mandate and stopping a
 * live session - must not look like ordinary buttons, and reaching for a raw colour
 * at the call site is how a component ends up referencing a token that does not
 * exist and rendering invisibly.
 */

import { cva, type VariantProps } from "class-variance-authority"
import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium transition-[background-color,opacity,box-shadow] outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-45 [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:opacity-90",
        secondary: "bg-secondary text-secondary-foreground hover:bg-accent",
        outline: "border border-input bg-transparent hover:bg-accent",
        ghost: "hover:bg-accent",
        link: "text-primary underline-offset-4 hover:underline",
        success: "bg-success text-background hover:opacity-90",
        destructive: "bg-danger text-background hover:opacity-90",
        // The emergency stop. Deliberately heavier than destructive.
        halt: "bg-danger text-background shadow-l hover:opacity-95 ring-1 ring-danger-border"
      },
      size: {
        default: "h-9 px-4 text-sm [&_svg]:size-4",
        sm: "h-8 px-3 text-xs [&_svg]:size-3.5",
        lg: "h-11 px-6 text-sm [&_svg]:size-4",
        xl: "h-14 px-8 text-base [&_svg]:size-5",
        icon: "size-9 [&_svg]:size-4"
      }
    },
    defaultVariants: { variant: "default", size: "default" }
  }
)

export type ButtonProps = ComponentProps<"button"> & VariantProps<typeof buttonVariants>

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button data-slot="button" className={cn(buttonVariants({ variant, size }), className)} {...props} />
}

export { buttonVariants }

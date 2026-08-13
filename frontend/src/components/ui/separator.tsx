/** shadcn separator, owned source, without the Radix dependency. */

import type { ComponentProps } from "react"

import { cn } from "@/lib/utils"

export interface SeparatorProps extends ComponentProps<"div"> {
  orientation?: "horizontal" | "vertical"
}

export function Separator({ className, orientation = "horizontal", ...props }: SeparatorProps) {
  return (
    <div
      data-slot="separator"
      role="separator"
      aria-orientation={orientation}
      className={cn(
        "shrink-0 bg-border",
        orientation === "horizontal" ? "h-px w-full" : "h-full w-px",
        className
      )}
      {...props}
    />
  )
}

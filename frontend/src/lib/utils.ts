/** The class helper, at the path shadcn's own generated components import it from.
 *
 * It lives here rather than in format.ts (where TradingAgent put it) purely so that
 * `npx shadcn add <component>` pastes files that resolve without editing: the CLI
 * hard-codes the `@/lib/utils` alias recorded in components.json.
 */

import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

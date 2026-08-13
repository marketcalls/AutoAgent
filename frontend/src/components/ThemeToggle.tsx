/** Light/dark switch.
 *
 * The class on <html> is the source of truth, not this component's state, because
 * the anti-FOUC script in index.html has already set it before React mounts. Reading
 * it back on first render is what keeps the two from disagreeing.
 */

import { useState } from "react"
import { Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"

const STORAGE_KEY = "aa-theme"

function currentlyDark(): boolean {
  if (typeof document === "undefined") return false
  return document.documentElement.classList.contains("dark")
}

export function ThemeToggle() {
  const [dark, setDark] = useState(currentlyDark)

  const toggle = () => {
    const next = !dark
    const root = document.documentElement
    root.classList.toggle("dark", next)
    root.classList.toggle("light", !next)
    try {
      localStorage.setItem(STORAGE_KEY, next ? "dark" : "light")
    } catch {
      // A blocked localStorage costs the preference on reload, nothing more.
    }
    setDark(next)
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggle}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      title={dark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {dark ? <Sun /> : <Moon />}
    </Button>
  )
}

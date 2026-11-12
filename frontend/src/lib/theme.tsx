import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'

export type ThemeChoice = 'system' | 'light' | 'dark'

const KEY = 'rgs-theme'

function systemDark(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-color-scheme: dark)').matches
}

function readChoice(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system'
  }
}

function apply(choice: ThemeChoice) {
  const dark = choice === 'dark' || (choice === 'system' && systemDark())
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
}

interface ThemeValue {
  choice: ThemeChoice
  setChoice: (c: ThemeChoice) => void
}

const ThemeContext = createContext<ThemeValue>({ choice: 'system', setChoice: () => {} })

/** System / light / dark, persisted; index.html applies it before first paint. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readChoice)

  useEffect(() => {
    apply(choice)
    if (choice !== 'system' || !window.matchMedia) return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => apply('system')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [choice])

  const setChoice = useCallback((c: ThemeChoice) => {
    try {
      if (c === 'system') localStorage.removeItem(KEY)
      else localStorage.setItem(KEY, c)
    } catch {
      /* private mode: the choice still applies for this tab */
    }
    setChoiceState(c)
  }, [])

  return <ThemeContext.Provider value={{ choice, setChoice }}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeValue {
  return useContext(ThemeContext)
}

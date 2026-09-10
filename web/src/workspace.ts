import { createContext, useContext } from 'react'
import type { RunAction } from './api'
import type { Session } from './types'

export interface WorkspaceValue {
  revision: number
  refresh: () => void
  run: RunAction
  busy: boolean
  session: Session
  navigate: (path: string) => void
}
export const WorkspaceContext = createContext<WorkspaceValue | null>(null)
export function useWorkspace() {
  const value = useContext(WorkspaceContext)
  if (!value) throw new Error('Workspace context is required')
  return value
}

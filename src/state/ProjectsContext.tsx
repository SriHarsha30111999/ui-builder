import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { ProjectsApi } from '../api/projectsApi'
import type { CreateProjectInput, CreateProjectResult, Project } from '../types/project'

type LoadState = 'loading' | 'ready' | 'error'

interface ProjectsContextValue {
  api: ProjectsApi
  projects: Project[]
  loadState: LoadState
  errorMessage?: string
  persistence: ProjectsApi['persistence']
  /** Reload with a visible loading state (used by "Try again"). */
  reload: () => void
  /** Re-fetch without flashing a loading state; keeps the current list if the request fails. */
  refresh: () => void
  createProject: (input: CreateProjectInput) => Promise<CreateProjectResult>
  /** Deletes on the server first; the list only changes once that succeeded. */
  deleteProject: (id: string) => Promise<void>
}

const ProjectsContext = createContext<ProjectsContextValue | null>(null)

export function ProjectsProvider({ api, children }: { api: ProjectsApi; children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [errorMessage, setErrorMessage] = useState<string>()
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    let cancelled = false
    api
      .listProjects()
      .then((list) => {
        if (cancelled) return
        setProjects(list)
        setErrorMessage(undefined)
        setLoadState('ready')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setErrorMessage(err instanceof Error ? err.message : 'Unknown error')
        setLoadState('error')
      })
    return () => {
      cancelled = true
    }
  }, [api, reloadToken])

  const reload = useCallback(() => {
    setLoadState('loading')
    setReloadToken((t) => t + 1)
  }, [])

  const refresh = useCallback(() => {
    api
      .listProjects()
      .then((list) => {
        setProjects(list)
        setErrorMessage(undefined)
        setLoadState('ready')
      })
      .catch((err: unknown) => {
        setErrorMessage(err instanceof Error ? err.message : 'Unknown error')
        setLoadState('error')
      })
  }, [api])

  const createProject = useCallback(
    async (input: CreateProjectInput) => {
      const result = await api.createProject(input)
      const { screenshots, ...summary } = result.project
      void screenshots
      setProjects((prev) => [summary, ...prev.filter((p) => p.id !== summary.id)])
      return result
    },
    [api],
  )

  const deleteProject = useCallback(
    async (id: string) => {
      await api.deleteProject(id)
      setProjects((prev) => prev.filter((p) => p.id !== id))
    },
    [api],
  )

  const value = useMemo(
    () => ({ api, projects, loadState, errorMessage, persistence: api.persistence, reload, refresh, createProject, deleteProject }),
    [api, projects, loadState, errorMessage, reload, refresh, createProject, deleteProject],
  )
  return <ProjectsContext.Provider value={value}>{children}</ProjectsContext.Provider>
}

export function useProjects() {
  const ctx = useContext(ProjectsContext)
  if (!ctx) throw new Error('useProjects must be used inside ProjectsProvider')
  return ctx
}

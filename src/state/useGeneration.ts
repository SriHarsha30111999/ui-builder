import { useCallback, useEffect, useState } from 'react'
import type { ProjectsApi } from '../api/projectsApi'
import { ACTIVE_STATUSES, type GeneratedVersion, type Generation, type GenerationReadiness, type StartGenerationInput } from '../types/generation'

const POLL_MS = 1500

const message = (err: unknown, fallback: string) => (err instanceof Error ? err.message : fallback)

/** Generation state for one project: readiness, versions, the latest session (polled while active). */
export function useGeneration(api: ProjectsApi, projectId: string) {
  const [loaded, setLoaded] = useState(false)
  const [readiness, setReadiness] = useState<GenerationReadiness | null>(null)
  const [versions, setVersions] = useState<GeneratedVersion[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [current, setCurrent] = useState<Generation | null>(null)
  const [history, setHistory] = useState<Generation[]>([]) // every session of the project, newest first (stored by the backend)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string>()

  useEffect(() => {
    let cancelled = false
    Promise.all([api.getGenerationReadiness().catch(() => null), api.listVersions(projectId), api.listGenerations(projectId)])
      .then(([r, v, g]) => {
        if (cancelled) return
        setReadiness(r)
        setVersions(v)
        setSelectedId(v[0]?.id ?? null)
        setHistory(g)
        setCurrent(g[0] ?? null) // resumes polling if the latest session is still running (e.g. after a page refresh)
        setLoaded(true)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(message(err, 'Could not load generation state.'))
        setLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [api, projectId])

  const active = current !== null && ACTIVE_STATUSES.includes(current.status)
  const activeId = active ? current.id : null

  const settle = useCallback(
    async (g: Generation) => {
      setCurrent(g)
      setHistory((h) => [g, ...h.filter((x) => x.id !== g.id)])
      if (g.status === 'completed') {
        const v = await api.listVersions(projectId)
        setVersions(v)
        setSelectedId(g.versionId ?? v[0]?.id ?? null)
      }
    },
    [api, projectId],
  )

  useEffect(() => {
    if (!activeId) return
    let stopped = false
    const timer = setInterval(() => {
      api
        .getGeneration(activeId)
        .then(async (g) => {
          if (stopped) return
          setError(undefined)
          if (!ACTIVE_STATUSES.includes(g.status)) clearInterval(timer)
          await settle(g)
        })
        .catch((err: unknown) => !stopped && setError(message(err, 'Could not read generation progress.')))
    }, POLL_MS)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [api, activeId, settle])

  const run = useCallback(
    async (action: () => Promise<Generation>) => {
      setStarting(true)
      setError(undefined)
      try {
        await settle(await action())
      } catch (err) {
        setError(message(err, 'Generation could not be started.'))
      } finally {
        setStarting(false)
      }
    },
    [settle],
  )

  return {
    loaded,
    readiness,
    versions,
    selectedId,
    selectVersion: setSelectedId,
    selected: versions.find((v) => v.id === selectedId) ?? null,
    current,
    history,
    active,
    starting,
    error,
    start: (input: StartGenerationInput) => run(() => api.startGeneration(projectId, input)),
    retry: (id: string) => run(() => api.retryGeneration(id)),
  }
}

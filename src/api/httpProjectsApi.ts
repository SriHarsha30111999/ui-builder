import { ApiError, type ProjectsApi } from './projectsApi'
import type { Project, ProjectDetail, ScreenshotMeta } from '../types/project'
import type { GeneratedVersion, Generation, GenerationReadiness } from '../types/generation'

export const DEFAULT_API_BASE_URL = 'http://localhost:8000/api'

interface ErrorBody {
  error?: { code?: string; message?: string; fields?: Record<string, string> }
}

/** HTTP implementation of ProjectsApi against the FastAPI backend. The only place that calls fetch. */
export function createHttpProjectsApi(baseUrl: string = DEFAULT_API_BASE_URL, fetchImpl: typeof fetch = (...a) => fetch(...a)): ProjectsApi {
  const base = baseUrl.replace(/\/+$/, '')
  const origin = new URL(base, window.location.href).origin

  async function request<T>(path: string, init?: RequestInit, options: { text?: boolean } = {}): Promise<T> {
    let res: Response
    try {
      res = await fetchImpl(`${base}${path}`, init)
    } catch {
      throw new ApiError(0, 'network_error', `Can't reach the backend API at ${base}. Is it running?`)
    }
    if (res.status === 204) return undefined as T
    let body: unknown
    try {
      body = options.text && res.ok ? await res.text() : await res.json()
    } catch {
      body = undefined
    }
    if (!res.ok) {
      const err = (body as ErrorBody | undefined)?.error
      throw new ApiError(res.status, err?.code ?? 'http_error', err?.message ?? `Request failed (${res.status}).`, err?.fields ?? {})
    }
    return body as T
  }

  const api: ProjectsApi = {
    persistence: 'server',
    listProjects: () => request<Project[]>('/projects'),
    getProject: (id) => request<ProjectDetail>(`/projects/${encodeURIComponent(id)}`),
    async createProject(input) {
      const created = await request<ProjectDetail>('/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: input.name,
          type: input.type,
          initialPrompt: input.initialPrompt,
          ...(input.type === 'feature' ? { masterId: input.masterId, basePath: input.basePath } : {}),
        }),
      })
      if (input.screenshots.length === 0) return { project: created }
      try {
        const screenshots = await api.uploadScreenshots(created.id, input.screenshots)
        return { project: { ...created, screenshots, screenshotCount: screenshots.length } }
      } catch (err) {
        // The project is saved; only the upload failed. Surface that honestly instead of failing the whole create.
        return { project: created, uploadError: err instanceof Error ? err.message : 'Screenshots could not be uploaded.' }
      }
    },
    uploadScreenshots(projectId, files) {
      const form = new FormData()
      files.forEach((f) => form.append('files', f, f.name))
      return request<ScreenshotMeta[]>(`/projects/${encodeURIComponent(projectId)}/screenshots`, { method: 'POST', body: form })
    },
    async deleteProject(id) {
      await request<void>(`/projects/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
    async deleteScreenshot(id) {
      await request<void>(`/screenshots/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
    screenshotSrc: (s) => `${origin}${s.url}`,

    getGenerationReadiness: () => request<GenerationReadiness>('/generation/status'),
    startGeneration: (projectId, input) =>
      request<Generation>(`/projects/${encodeURIComponent(projectId)}/generations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...(input.prompt?.trim() ? { prompt: input.prompt.trim() } : {}),
          screenshotIds: input.screenshotIds,
          ...(input.parentVersionId ? { parentVersionId: input.parentVersionId } : {}),
        }),
      }),
    getGeneration: (id) => request<Generation>(`/generations/${encodeURIComponent(id)}`),
    retryGeneration: (id) => request<Generation>(`/generations/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
    listGenerations: (projectId) => request<Generation[]>(`/projects/${encodeURIComponent(projectId)}/generations`),
    async getBuildLog(id) {
      return (await request<{ log: string }>(`/generations/${encodeURIComponent(id)}/build-log`)).log
    },
    listVersions: (projectId) => request<GeneratedVersion[]>(`/projects/${encodeURIComponent(projectId)}/versions`),
    getVersionFile: (versionId, path) =>
      request<string>(`/versions/${encodeURIComponent(versionId)}/files/${path.split('/').map(encodeURIComponent).join('/')}`, undefined, { text: true }),
    previewSrc: (v) => `${origin}${v.previewUrl}`,
    downloadUrl: (v) => `${origin}/api/versions/${encodeURIComponent(v.id)}/download`,
  }
  return api
}

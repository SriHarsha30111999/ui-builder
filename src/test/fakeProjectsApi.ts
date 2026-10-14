import { ApiError, type ProjectsApi } from '../api/projectsApi'
import type { Project, ProjectDetail, ScreenshotMeta } from '../types/project'
import type { GeneratedVersion, Generation, GenerationReadiness, StartGenerationInput } from '../types/generation'
import { normalizeBasePath } from '../validation'

/**
 * TEST DOUBLE ONLY. Used by unit tests to exercise the UI without a server.
 * It is not wired into the application and does not demonstrate backend behavior.
 */
export interface FakeControls {
  projects: ProjectDetail[]
  readiness: GenerationReadiness
  /** Next startGeneration ends as a failed session with this error instead of completing. */
  failNext?: { code: string; message: string; retryable: boolean }
  /** Inputs received by startGeneration, in order. */
  started: { projectId: string; input: StartGenerationInput }[]
  generations: Generation[]
  versions: GeneratedVersion[]
}

export function createFakeProjectsApi(): ProjectsApi & FakeControls {
  const projects: ProjectDetail[] = []
  const generations: Generation[] = []
  const versions: GeneratedVersion[] = []
  const started: FakeControls['started'] = []
  const readiness: GenerationReadiness = {
    aiConfigured: true, codeModel: 'code-model', visionModel: 'vision-model', buildRunner: 'fake', buildAvailable: true, buildMessage: 'ok', ready: true, maxSessionAttempts: 3,
  }
  const toSummary = (p: ProjectDetail): Project => {
    const { screenshots, ...rest } = p
    void screenshots
    return rest
  }
  const find = (id: string) => projects.find((p) => p.id === id)

  async function uploadScreenshots(projectId: string, files: File[]): Promise<ScreenshotMeta[]> {
    const project = find(projectId)
    if (!project) throw new ApiError(404, 'project_not_found', 'Project not found.')
    const metas = files.map((f): ScreenshotMeta => {
      const id = crypto.randomUUID()
      return { id, projectId, filename: f.name, contentType: f.type, sizeBytes: f.size, createdAt: new Date().toISOString(), url: `/api/screenshots/${id}` }
    })
    project.screenshots.push(...metas)
    project.screenshotCount = project.screenshots.length
    return metas
  }

  const api: ProjectsApi & FakeControls = {
    projects,
    generations,
    versions,
    started,
    readiness,
    persistence: 'session-only',
    async listProjects() {
      return projects.map(toSummary)
    },
    async getProject(id) {
      const p = find(id)
      if (!p) throw new ApiError(404, 'project_not_found', 'Project not found.')
      return structuredClone(p)
    },
    async createProject(input) {
      const now = new Date().toISOString()
      const basePath = input.type === 'feature' ? normalizeBasePath(input.basePath ?? '') : null
      const masterId = input.type === 'feature' ? (input.masterId ?? null) : null
      if (basePath && projects.some((p) => p.masterId === masterId && p.basePath === basePath)) {
        const msg = `Another feature under this Master already uses "${basePath}".`
        throw new ApiError(409, 'base_path_conflict', msg, { basePath: msg })
      }
      const project: ProjectDetail = {
        id: crypto.randomUUID(),
        name: input.name.trim(),
        type: input.type,
        masterId,
        masterName: projects.find((p) => p.id === masterId)?.name ?? null,
        basePath,
        initialPrompt: input.initialPrompt.trim(),
        screenshotCount: 0,
        status: 'draft',
        createdAt: now,
        updatedAt: now,
        screenshots: [],
      }
      projects.push(project)
      if (input.screenshots.length) await uploadScreenshots(project.id, input.screenshots)
      return { project: structuredClone(project) }
    },
    uploadScreenshots,
    async deleteProject(id) {
      const i = projects.findIndex((p) => p.id === id)
      if (i < 0) throw new ApiError(404, 'project_not_found', 'Project not found.')
      if (projects.some((p) => p.masterId === id)) throw new ApiError(409, 'project_has_features', 'This Master Project still has Feature Projects. Delete those first.')
      projects.splice(i, 1)
      for (const list of [versions, generations]) {
        for (let k = list.length - 1; k >= 0; k--) if (list[k].projectId === id) list.splice(k, 1)
      }
    },
    async deleteScreenshot(id) {
      for (const p of projects) {
        const i = p.screenshots.findIndex((s) => s.id === id)
        if (i >= 0) {
          p.screenshots.splice(i, 1)
          p.screenshotCount = p.screenshots.length
          return
        }
      }
      throw new ApiError(404, 'screenshot_not_found', 'Screenshot not found.')
    },
    screenshotSrc: (s) => `blob:fake/${s.id}`,

    async getGenerationReadiness() {
      return { ...api.readiness }
    },
    async startGeneration(projectId, input) {
      if (!api.readiness.aiConfigured) throw new ApiError(503, 'ai_not_configured', 'No AI provider token is configured.')
      started.push({ projectId, input })
      const now = new Date().toISOString()
      const prompt = input.prompt?.trim() || null
      const base: Generation = {
        id: crypto.randomUUID(), projectId, parentVersionId: input.parentVersionId ?? null, versionId: null,
        inputMode: prompt && input.screenshotIds.length ? 'prompt_and_screenshot' : prompt ? 'prompt_only' : 'screenshot_only',
        prompt, screenshotIds: input.screenshotIds, status: 'completed', attempts: 1, errorCode: null, errorMessage: null,
        retryable: false, modelId: 'fake-model', createdAt: now, startedAt: now, completedAt: now, updatedAt: now,
      }
      if (api.failNext) {
        const f = api.failNext
        api.failNext = undefined
        Object.assign(base, { status: 'failed', errorCode: f.code, errorMessage: f.message, retryable: f.retryable })
      } else {
        const v: GeneratedVersion = {
          id: crypto.randomUUID(), projectId, sessionId: base.id, parentVersionId: base.parentVersionId,
          versionNumber: versions.filter((x) => x.projectId === projectId).length + 1, summary: 'Fake summary', uncertainties: [],
          files: [{ path: 'src/App.tsx', size: 10 }], modelId: 'fake-model', repaired: false, createdAt: now, previewUrl: `/api/previews/${base.id}/`,
        }
        versions.push(v)
        base.versionId = v.id
      }
      generations.push(base)
      return structuredClone(base)
    },
    async getGeneration(id) {
      const g = generations.find((x) => x.id === id)
      if (!g) throw new ApiError(404, 'generation_not_found', 'Generation not found.')
      return structuredClone(g)
    },
    async retryGeneration(id) {
      return api.getGeneration(id)
    },
    async listGenerations(projectId) {
      return generations.filter((g) => g.projectId === projectId).reverse().map((g) => structuredClone(g))
    },
    async getBuildLog() {
      return ''
    },
    async listVersions(projectId) {
      return versions.filter((v) => v.projectId === projectId).reverse().map((v) => structuredClone(v))
    },
    async getVersionFile(_versionId, path) {
      return `// contents of ${path}`
    },
    previewSrc: (v) => `blob:fake-preview/${v.id}`,
    downloadUrl: (v) => `http://fake.test/api/versions/${v.id}/download`,
  }
  return api
}

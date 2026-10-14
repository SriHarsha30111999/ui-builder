import type { GeneratedVersion, Generation, GenerationReadiness, StartGenerationInput } from '../types/generation'
import type { CreateProjectInput, CreateProjectResult, Project, ProjectDetail, ScreenshotMeta } from '../types/project'

/** Error from the API layer. `fields` carries per-field messages (keys match form fields) when the server sent them. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly fields: Record<string, string>

  constructor(status: number, code: string, message: string, fields: Record<string, string> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.fields = fields
  }
}

/** Boundary between the UI and project persistence. The UI depends only on this interface. */
export interface ProjectsApi {
  /** Where data lives; surfaced in the UI so non-persistent state is never mistaken for saved data. */
  readonly persistence: 'session-only' | 'server'
  listProjects(): Promise<Project[]>
  /** Rejects with ApiError (status 404) when the project does not exist. */
  getProject(id: string): Promise<ProjectDetail>
  createProject(input: CreateProjectInput): Promise<CreateProjectResult>
  /** Deletes the project with its generations, versions, screenshots and source. Rejects (409) while a generation runs or a Master still has Features. */
  deleteProject(id: string): Promise<void>
  uploadScreenshots(projectId: string, files: File[]): Promise<ScreenshotMeta[]>
  deleteScreenshot(screenshotId: string): Promise<void>
  /** URL the browser can load the image from. */
  screenshotSrc(screenshot: ScreenshotMeta): string

  // --- AI generation ---
  /** Whether the backend has an AI provider token and a build sandbox. No secrets are returned. */
  getGenerationReadiness(): Promise<GenerationReadiness>
  /** Starts a background generation; poll getGeneration for progress. Rejects with ApiError (e.g. ai_not_configured). */
  startGeneration(projectId: string, input: StartGenerationInput): Promise<Generation>
  getGeneration(id: string): Promise<Generation>
  retryGeneration(id: string): Promise<Generation>
  listGenerations(projectId: string): Promise<Generation[]>
  getBuildLog(generationId: string): Promise<string>
  listVersions(projectId: string): Promise<GeneratedVersion[]>
  getVersionFile(versionId: string, path: string): Promise<string>
  /** URL of the built preview, to be loaded only inside a sandboxed iframe. */
  previewSrc(version: GeneratedVersion): string
  /** URL that downloads the version's source as a zip (navigate to it; the server sends Content-Disposition: attachment). */
  downloadUrl(version: GeneratedVersion): string
}

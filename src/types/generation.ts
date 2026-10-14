export type InputMode = 'prompt_only' | 'screenshot_only' | 'prompt_and_screenshot'

export type GenerationStatus = 'created' | 'queued' | 'analyzing' | 'generating' | 'building' | 'repairing' | 'completed' | 'failed'

export const ACTIVE_STATUSES: GenerationStatus[] = ['created', 'queued', 'analyzing', 'generating', 'building', 'repairing']

export interface Generation {
  id: string
  projectId: string
  parentVersionId: string | null
  /** Set only when the session completed and its build succeeded. */
  versionId: string | null
  inputMode: InputMode
  prompt: string | null
  screenshotIds: string[]
  status: GenerationStatus
  attempts: number
  errorCode: string | null
  errorMessage: string | null
  retryable: boolean
  modelId: string | null
  createdAt: string
  startedAt: string | null
  completedAt: string | null
  updatedAt: string
}

export interface VersionFile {
  path: string
  size: number
}

export interface GeneratedVersion {
  id: string
  projectId: string
  sessionId: string
  parentVersionId: string | null
  versionNumber: number
  summary: string
  uncertainties: string[]
  files: VersionFile[]
  modelId: string
  repaired: boolean
  createdAt: string
  /** Path on the API origin; use ProjectsApi.previewSrc to get a loadable URL. */
  previewUrl: string
}

/** What the backend can currently do. No secrets: only whether a token is set. */
export interface GenerationReadiness {
  aiConfigured: boolean
  codeModel: string
  visionModel: string
  buildRunner: string
  buildAvailable: boolean
  buildMessage: string
  ready: boolean
  maxSessionAttempts: number
}

export interface StartGenerationInput {
  prompt?: string
  screenshotIds: string[]
  /** Set to refine an existing version instead of starting from scratch. */
  parentVersionId?: string
}

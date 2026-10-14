export type ProjectType = 'master' | 'feature'

/** Status vocabulary for the full product. Phases 1-2 only ever produce 'draft'. */
export type ProjectStatus = 'draft' | 'generating' | 'build_failed' | 'ready' | 'published'

export interface Project {
  id: string
  name: string
  type: ProjectType
  /** Feature projects only. */
  masterId?: string | null
  masterName?: string | null
  /** Feature projects only: path the feature mounts under in the Master. */
  basePath?: string | null
  initialPrompt: string
  screenshotCount: number
  status: ProjectStatus
  createdAt: string
  updatedAt: string
}

/** Metadata of a screenshot stored by the backend. The image bytes are fetched from `url`. */
export interface ScreenshotMeta {
  id: string
  projectId: string
  filename: string
  contentType: string
  sizeBytes: number
  createdAt: string
  url: string
}

export interface ProjectDetail extends Project {
  screenshots: ScreenshotMeta[]
}

export interface CreateProjectInput {
  name: string
  type: ProjectType
  initialPrompt: string
  /** Files chosen in the form; uploaded after the project is created. */
  screenshots: File[]
  masterId?: string
  basePath?: string
}

export interface CreateProjectResult {
  /** The project exists (it was saved) even when `uploadError` is set. */
  project: ProjectDetail
  uploadError?: string
}

export const SOURCE_CONTROL_PROVIDERS = ['GitHub', 'GitLab', 'Bitbucket', 'Azure DevOps'] as const
export type SourceControlProvider = (typeof SOURCE_CONTROL_PROVIDERS)[number]

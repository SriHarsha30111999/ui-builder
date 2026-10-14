import type { CreateProjectInput, Project } from './types/project'

export type ProjectFormErrors = Partial<Record<'name' | 'initialPrompt' | 'masterId' | 'basePath', string>>

/** Routes owned by the Master shell; features may not claim them. Compared against the normalized path. */
export const RESERVED_BASE_PATHS = ['/login', '/logout', '/dashboard', '/auth', '/api', '/404'] as const

/**
 * Normalizes a base path: trims, lowercases, collapses repeated slashes, ensures one leading slash and no trailing slash.
 * Returns '' when nothing usable remains (e.g. empty input or "/").
 */
export function normalizeBasePath(input: string): string {
  const segments = input.trim().toLowerCase().split('/').filter(Boolean)
  return segments.length ? '/' + segments.join('/') : ''
}

export const MAX_BASE_PATH_LENGTH = 100
const SEGMENT =/^[a-z0-9]+(?:[-_][a-z0-9]+)*$/

/** Early feedback only. The backend re-validates and is authoritative (including per-Master uniqueness). */
export function validateBasePath(input: string, masterId: string | undefined, existing: Project[]): string | undefined {
  if (!input.trim()) return 'Enter the base path for this feature, e.g. /accounts.'
  if (/[\s?#:*\\%]/.test(input.trim())) return 'Base path cannot contain spaces or the characters ? # : * \\ %'
  const normalized = normalizeBasePath(input)
  if (!normalized) return 'Base path must name a route below the root, e.g. /accounts.'
  if (normalized.length > MAX_BASE_PATH_LENGTH) return `Base path must be ${MAX_BASE_PATH_LENGTH} characters or fewer.`
  if (!normalized.slice(1).split('/').every((seg) => SEGMENT.test(seg))) {
    return 'Use letters, numbers, "-" and "_" in each path segment (no empty or "." segments).'
  }
  if (RESERVED_BASE_PATHS.some((r) => normalized === r || normalized.startsWith(r + '/'))) {
    return `"${normalized}" is reserved for the Master application. Choose another path.`
  }
  if (masterId && existing.some((p) => p.type === 'feature' && p.masterId === masterId && p.basePath === normalized)) {
    return `Another feature under this Master already uses "${normalized}".`
  }
  return undefined
}

export function validateProjectForm(v: CreateProjectInput, existing: Project[] = []): ProjectFormErrors {
  const errors: ProjectFormErrors = {}
  const name = v.name.trim()
  if (!name) errors.name = 'Enter a project name.'
  else if (name.length < 3) errors.name = 'Project name must be at least 3 characters.'
  else if (name.length > 60) errors.name = 'Project name must be 60 characters or fewer.'

  if (!v.initialPrompt.trim() && v.screenshots.length === 0) {
    errors.initialPrompt = 'Describe the UI you want, or upload at least one screenshot.'
  }

  if (v.type === 'feature') {
    if (!v.masterId) errors.masterId = 'Select the Master Project this feature belongs to.'
    const pathError = validateBasePath(v.basePath ?? '', v.masterId, existing)
    if (pathError) errors.basePath = pathError
  }
  return errors
}

export const MAX_SCREENSHOTS = 4 // per prompt (mirrors MAX_SCREENSHOTS_PER_PROMPT in the backend settings)
export const MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
const ACCEPTED = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']

/** Returns an error message, or undefined if the file is acceptable. */
export function validateScreenshot(file: File): string | undefined {
  if (!ACCEPTED.includes(file.type)) return `"${file.name}" is not a supported image (use PNG, JPEG, WebP or GIF).`
  if (file.size > MAX_SCREENSHOT_BYTES) return `"${file.name}" is larger than 10 MB.`
  return undefined
}

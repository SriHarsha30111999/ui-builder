import { Badge, type Tone } from '../ui/primitives'
import type { ProjectStatus, ProjectType } from '../../types/project'

const STATUS: Record<ProjectStatus, { label: string; tone: Tone }> = {
  draft: { label: 'Draft', tone: 'neutral' },
  generating: { label: 'Generating', tone: 'info' },
  build_failed: { label: 'Build failed', tone: 'danger' },
  ready: { label: 'Ready', tone: 'success' },
  published: { label: 'Published', tone: 'primary' },
}

export function StatusBadge({ status }: { status: ProjectStatus }) {
  const s = STATUS[status]
  return <Badge tone={s.tone}>{s.label}</Badge>
}

export function TypeBadge({ type }: { type: ProjectType }) {
  return <Badge tone={type === 'master' ? 'primary' : 'info'}>{type === 'master' ? 'Master' : 'Feature'}</Badge>
}

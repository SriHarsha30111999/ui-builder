import { useState } from 'react'
import type { ProjectsApi } from '../../api/projectsApi'
import type { GeneratedVersion, Generation, GenerationStatus } from '../../types/generation'
import { Badge, Button, SegmentedControl, Spinner, type Tone } from '../ui/primitives'

export type Viewport = 'desktop' | 'tablet' | 'mobile'
const VIEWPORT_WIDTH: Record<Viewport, string> = { desktop: '100%', tablet: '768px', mobile: '375px' }

export const STAGE_LABEL: Record<GenerationStatus, string> = {
  created: 'Created',
  queued: 'Queued',
  analyzing: 'Analyzing screenshots with the vision model',
  generating: 'Generating code with the model',
  building: 'Building in the isolated sandbox',
  repairing: 'Repairing build errors (single bounded attempt)',
  completed: 'Completed',
  failed: 'Failed',
}

function statusBadges(current: Generation | null, hasPreview: boolean): { generation: [string, Tone]; build: [string, Tone] } {
  if (!current) return { generation: ['Not started', 'neutral'], build: [hasPreview ? 'Built' : 'Not built', hasPreview ? 'success' : 'neutral'] }
  switch (current.status) {
    case 'completed':
      return { generation: ['Completed', 'success'], build: ['Succeeded', 'success'] }
    case 'failed': {
      const buildFailed = ['build_failed', 'build_timeout', 'invalid_build_output'].includes(current.errorCode ?? '')
      return { generation: ['Failed', 'danger'], build: [buildFailed ? 'Failed' : hasPreview ? 'Previous version kept' : 'Not built', buildFailed ? 'danger' : 'neutral'] }
    }
    case 'building':
    case 'repairing':
      return { generation: ['In progress', 'info'], build: ['Running', 'info'] }
    default:
      return { generation: ['In progress', 'info'], build: ['Waiting', 'neutral'] }
  }
}

interface Props {
  api: ProjectsApi
  current: Generation | null
  version: GeneratedVersion | null
}

/** Shows only real state: the sandboxed iframe loads a build the backend produced, or an explicit empty/progress/error state. */
export function PreviewPanel({ api, current, version }: Props) {
  const [viewport, setViewport] = useState<Viewport>('desktop')
  const [reloads, setReloads] = useState(0)
  const running = current !== null && !['completed', 'failed'].includes(current.status)
  const badges = statusBadges(current, version !== null)
  const failedNow = current?.status === 'failed'

  return (
    <div className="builder__right">
      <div className="preview-toolbar" role="toolbar" aria-label="Preview controls">
        <SegmentedControl
          label="Viewport"
          value={viewport}
          onChange={setViewport}
          options={[
            { value: 'desktop', label: 'Desktop' },
            { value: 'tablet', label: 'Tablet' },
            { value: 'mobile', label: 'Mobile' },
          ]}
        />
        <Button size="sm" disabled={!version} onClick={() => setReloads((n) => n + 1)} title={version ? 'Reload the preview' : 'No preview yet'}>
          ↻ Refresh
        </Button>
        <span className="preview-toolbar__spacer" />
        <div className="status-row" aria-label="Status">
          <span className="muted">Generation</span>
          <Badge tone={badges.generation[1]}>{badges.generation[0]}</Badge>
          <span className="muted">Build</span>
          <Badge tone={badges.build[1]}>{badges.build[0]}</Badge>
        </div>
      </div>

      <div className="preview-stage">
        <div
          className={`preview-frame${version ? ' preview-frame--live' : ''}`}
          style={{ width: VIEWPORT_WIDTH[viewport], maxWidth: '100%' }}
          aria-label={`Preview area, ${viewport} viewport`}
        >
          {version ? (
            // sandbox without allow-same-origin: generated code gets an opaque origin and cannot reach the app's storage or API.
            <iframe
              key={`${version.id}-${reloads}`}
              title={`Preview of version ${version.versionNumber}`}
              src={api.previewSrc(version)}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              className="preview-iframe"
            />
          ) : running ? (
            <>
              <Spinner label="Generating" />
              <h2>{current ? STAGE_LABEL[current.status] : 'Working…'}</h2>
              <p>The preview appears here once the build succeeds.</p>
            </>
          ) : (
            <>
              <h2>{failedNow ? 'No preview available' : 'No preview yet'}</h2>
              <p>
                {failedNow
                  ? 'The last generation did not produce a working build, so nothing is shown here.'
                  : 'Describe the UI, add screenshots, or both, then choose Generate UI. A screenshot is optional.'}
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

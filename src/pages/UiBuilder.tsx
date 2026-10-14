import { useEffect, useRef, useState } from 'react'
import { useLocation, useParams } from 'react-router-dom'
import { PreviewPanel, STAGE_LABEL } from '../components/builder/PreviewPanel'
import { PublishPanel } from '../components/builder/PublishPanel'
import { StatusBadge, TypeBadge } from '../components/projects/badges'
import { Banner, Button, Field, LinkButton, StateBox } from '../components/ui/primitives'
import { ApiError, type ProjectsApi } from '../api/projectsApi'
import { useProjects } from '../state/ProjectsContext'
import { useGeneration } from '../state/useGeneration'
import { ACTIVE_STATUSES } from '../types/generation'
import { MAX_SCREENSHOTS, validateScreenshot } from '../validation'
import type { ProjectDetail, ScreenshotMeta } from '../types/project'

function Builder({ project, api, uploadNotice }: { project: ProjectDetail; api: ProjectsApi; uploadNotice?: string }) {
  const masterName = project.masterName ?? undefined
  // The composer text is local until sent (the prompt is then stored with that generation session and shown in the thread).
  const [prompt, setPrompt] = useState(project.initialPrompt)
  const [screenshots, setScreenshots] = useState<ScreenshotMeta[]>(project.screenshots)
  const [saving, setSaving] = useState(false)
  const [shotError, setShotError] = useState<string>(uploadNotice ?? '')
  const [generateError, setGenerateError] = useState<string>()
  const gen = useGeneration(api, project.id)
  const [buildLog, setBuildLog] = useState<{ id: string; text: string }>()
  const fileInput = useRef<HTMLInputElement>(null)
  const threadEnd = useRef<HTMLDivElement>(null)

  // Oldest first, like a chat. A screenshot belongs to the turn that sent it; the rest are still waiting in the composer.
  const turns = [...gen.history].reverse()
  const sentIds = new Set(gen.history.flatMap((g) => g.screenshotIds))
  const pending = screenshots.filter((s) => !sentIds.has(s.id))
  const shotById = new Map(screenshots.map((s) => [s.id, s]))
  const refining = gen.selected !== null

  useEffect(() => {
    threadEnd.current?.scrollIntoView?.({ block: 'end' })
  }, [gen.history.length, gen.current?.status])

  async function saveShots(action: () => Promise<void>) {
    setSaving(true)
    setShotError('')
    try {
      await action()
    } catch (err) {
      setShotError(err instanceof Error ? err.message : 'The screenshot change could not be saved.')
    } finally {
      setSaving(false)
    }
  }

  function addShots(files: File[]) {
    const problems: string[] = []
    const valid: File[] = []
    for (const f of files) {
      const err = validateScreenshot(f)
      if (err) problems.push(err)
      else valid.push(f)
    }
    const room = MAX_SCREENSHOTS - pending.length
    if (valid.length > room) problems.push(`At most ${MAX_SCREENSHOTS} screenshots can wait in one message; ${valid.length - room} not added.`)
    if (problems.length) setShotError(problems.join(' '))
    if (valid.length && room > 0) {
      void saveShots(async () => {
        const added = await api.uploadScreenshots(project.id, valid.slice(0, room))
        setScreenshots((prev) => [...prev, ...added])
      })
    }
  }

  const removeShot = (target: ScreenshotMeta) =>
    saveShots(async () => {
      await api.deleteScreenshot(target.id)
      setScreenshots((prev) => prev.filter((s) => s.id !== target.id))
    })

  const notReady = gen.readiness !== null && !gen.readiness.ready
  const busy = gen.active || gen.starting

  function onSend() {
    const text = prompt.trim()
    if (!text && pending.length === 0) {
      setGenerateError(refining ? 'Describe the change you want to make, or add a new screenshot.' : 'Add a prompt, or at least one screenshot, before generating.')
      return
    }
    setGenerateError(undefined)
    if (refining && gen.selected) {
      void gen.start({
        prompt: text || 'Update the UI to match the attached screenshot(s).',
        screenshotIds: pending.map((s) => s.id),
        parentVersionId: gen.selected.id,
      })
    } else {
      void gen.start({ prompt: text || undefined, screenshotIds: pending.map((s) => s.id) })
    }
    setPrompt('')
  }

  async function showBuildLog(id: string) {
    try {
      setBuildLog({ id, text: (await api.getBuildLog(id)) || '(no build output was recorded)' })
    } catch (err) {
      setBuildLog({ id, text: err instanceof Error ? err.message : 'Could not load the build log.' })
    }
  }

  const shownError = generateError ?? gen.error

  return (
    <div className="builder">
      <div className="builder__context">
        <LinkButton to="/" variant="ghost" size="sm">
          ← Projects
        </LinkButton>
        <h1>{project.name}</h1>
        <TypeBadge type={project.type} />
        <StatusBadge status={project.status} />
        {project.type === 'feature' && (
          <span className="muted">
            Feature of <strong>{masterName ?? 'unknown Master'}</strong> · mounts at <code>{project.basePath}</code>
          </span>
        )}
      </div>

      <div className="builder__body">
        <aside className="builder__left chat" aria-label="Conversation">
          <div className="chat__thread">
            {notReady && gen.readiness && (
              <Banner tone="danger" title="Generation is not available">
                {!gen.readiness.aiConfigured && (
                  <p>No AI provider token is configured. Set AI_API_KEY (or HF_TOKEN) in the backend .env and restart it. See docs/PHASE3.md.</p>
                )}
                {!gen.readiness.buildAvailable && <p>{gen.readiness.buildMessage}</p>}
              </Banner>
            )}

            <details className="chat__info">
              <summary>Project context</summary>
              <dl className="kv">
                <dt>Mode</dt>
                <dd>{project.type === 'master' ? 'Master Project' : 'Feature Project'}</dd>
                {project.type === 'feature' && (
                  <>
                    <dt>Master</dt>
                    <dd>{masterName ?? 'Unknown'}</dd>
                    <dt>Base path</dt>
                    <dd>{project.basePath}</dd>
                  </>
                )}
                <dt>Created</dt>
                <dd>{new Date(project.createdAt).toLocaleString()}</dd>
              </dl>
              <PublishPanel />
            </details>

            {gen.loaded && turns.length === 0 && (
              <p className="muted">Describe the UI you want below, optionally with screenshots. Each message you send continues this project as a new version.</p>
            )}

            {turns.map((g) => {
              const v = gen.versions.find((x) => x.id === g.versionId)
              const shots = g.screenshotIds.map((id) => shotById.get(id)).filter((s): s is ScreenshotMeta => s !== undefined)
              const isLatest = g.id === gen.current?.id
              return (
                <div key={g.id} className="chat__turn">
                  <div className="chat__msg chat__msg--user">
                    {shots.length > 0 && (
                      <div className="chat__shots">
                        {shots.map((s) => (
                          <img key={s.id} src={api.screenshotSrc(s)} alt={`Sent screenshot ${s.filename}`} />
                        ))}
                      </div>
                    )}
                    <p>{g.prompt || '(screenshot only)'}</p>
                    <span className="muted">{g.parentVersionId ? 'Refinement' : 'First generation'}</span>
                  </div>
                  <div className="chat__msg chat__msg--ai">
                    {ACTIVE_STATUSES.includes(g.status) && <p>{STAGE_LABEL[g.status]}…</p>}
                    {v && (
                      <>
                        <p>
                          <strong>Version {v.versionNumber}</strong> · {v.summary}
                        </p>
                        {v.repaired && <p className="muted">One automatic repair was needed to make this build succeed.</p>}
                        {v.uncertainties.length > 0 && (
                          <ul>
                            {v.uncertainties.map((u) => (
                              <li key={u}>{u}</li>
                            ))}
                          </ul>
                        )}
                        <div className="chat__actions">
                          <Button size="sm" variant="ghost" disabled={gen.selectedId === v.id} onClick={() => gen.selectVersion(v.id)}>
                            {gen.selectedId === v.id ? 'Showing in Preview' : 'Show in Preview'}
                          </Button>
                          <a className="btn btn--sm" href={api.downloadUrl(v)} download>
                            Download source
                          </a>
                        </div>
                      </>
                    )}
                    {g.status === 'failed' && (
                      <Banner tone="danger" title="Generation failed">
                        <p>{g.errorMessage ?? 'Generation failed.'}</p>
                        <p className="muted">Code: {g.errorCode}</p>
                        {isLatest && g.retryable && g.attempts < (gen.readiness?.maxSessionAttempts ?? 3) && (
                          <Button size="sm" disabled={busy || notReady} onClick={() => void gen.retry(g.id)}>
                            Retry
                          </Button>
                        )}
                        {!g.retryable && <p>Adjust the prompt or screenshots and send again.</p>}
                        {['build_failed', 'invalid_build_output', 'build_timeout'].includes(g.errorCode ?? '') && (
                          <Button size="sm" onClick={() => void showBuildLog(g.id)}>
                            Show build log
                          </Button>
                        )}
                        {buildLog?.id === g.id && <pre className="error-log">{buildLog.text}</pre>}
                      </Banner>
                    )}
                  </div>
                </div>
              )
            })}

            <div ref={threadEnd} />
          </div>

          <div className="chat__composer">
            {shotError && (
              <Banner tone="danger" title="Screenshot problem">
                {shotError}
              </Banner>
            )}
            {pending.length > 0 && (
              <div className="chat__pending" aria-label="Screenshots waiting to be sent">
                {pending.map((s) => (
                  <span key={s.id} className="chat__chip">
                    <img src={api.screenshotSrc(s)} alt={`Preview of ${s.filename}`} />
                    <button type="button" aria-label={`Remove ${s.filename}`} disabled={saving} onClick={() => void removeShot(s)}>
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}
            <Field
              label={refining ? 'Additional prompt' : 'Prompt'}
              hint={refining ? `Applied to version ${gen.selected?.versionNumber}; the result becomes a new version.` : 'Optional if you add a screenshot.'}
            >
              {(a) => (
                <textarea
                  {...a}
                  className="textarea"
                  style={{ minHeight: 72 }}
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder={refining ? 'e.g. Change the theme to blue…' : 'Describe the UI you want…'}
                />
              )}
            </Field>
            {shownError && (
              <span className="field__error" role="alert">
                {shownError}
              </span>
            )}
            <div className="chat__actions">
              <input
                ref={fileInput}
                type="file"
                hidden
                multiple
                accept="image/png,image/jpeg,image/webp,image/gif"
                aria-label="Upload screenshots"
                onChange={(e) => {
                  addShots(Array.from(e.target.files ?? []))
                  e.target.value = ''
                }}
              />
              <Button size="sm" disabled={saving || busy} onClick={() => fileInput.current?.click()}>
                Attach image
              </Button>
              <Button variant="primary" onClick={onSend} disabled={busy || notReady}>
                {refining ? 'Apply changes' : 'Generate UI'}
              </Button>
            </div>
          </div>
        </aside>

        <PreviewPanel api={api} current={gen.current} version={gen.selected} />
      </div>
    </div>
  )
}

/** Keyed by project id so navigating between projects starts from a clean loading state. */
export function UiBuilder() {
  const { id } = useParams()
  return <LoadedBuilder key={id} id={id ?? ''} />
}

function LoadedBuilder({ id }: { id: string }) {
  const { api } = useProjects()
  const location = useLocation()
  const [state, setState] = useState<{ status: 'loading' } | { status: 'ready'; project: ProjectDetail } | { status: 'missing' } | { status: 'error'; message: string }>({ status: 'loading' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    api
      .getProject(id)
      .then((project) => !cancelled && setState({ status: 'ready', project }))
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && (err.status === 404 || err.status === 422)) setState({ status: 'missing' })
        else setState({ status: 'error', message: err instanceof Error ? err.message : 'Could not load the project.' })
      })
    return () => {
      cancelled = true
    }
  }, [api, id, attempt])

  if (state.status !== 'ready') {
    const title = state.status === 'loading' ? 'Loading…' : state.status === 'missing' ? 'Project not found' : "Couldn't load project"
    return (
      <div className="page">
        <StateBox
          tone={state.status === 'loading' ? undefined : 'danger'}
          title={title}
          action={
            state.status === 'error' ? (
              <Button
                onClick={() => {
                  setState({ status: 'loading' })
                  setAttempt((n) => n + 1)
                }}
              >
                Try again
              </Button>
            ) : (
              <LinkButton to="/">Back to projects</LinkButton>
            )
          }
        >
          {state.status === 'missing'
            ? 'No project with this ID exists. It may have been mistyped or never created.'
            : state.status === 'error'
              ? state.message
              : 'Fetching the project from the server.'}
        </StateBox>
      </div>
    )
  }
  const uploadNotice = (location.state as { uploadError?: string } | null)?.uploadError
  return <Builder key={state.project.id} project={state.project} api={api} uploadNotice={uploadNotice ? `The project was created, but its screenshots were not uploaded: ${uploadNotice}` : undefined} />
}

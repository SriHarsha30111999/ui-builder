import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Banner, Button, Field, LinkButton } from '../components/ui/primitives'
import { ScreenshotUploader, useLocalFileItems } from '../components/ui/ScreenshotUploader'
import { ApiError } from '../api/projectsApi'
import { useProjects } from '../state/ProjectsContext'
import type { CreateProjectInput, ProjectType } from '../types/project'
import { normalizeBasePath, validateProjectForm, type ProjectFormErrors } from '../validation'

const TYPE_OPTIONS: { value: ProjectType; title: string; blurb: string }[] = [
  { value: 'master', title: 'Master Project', blurb: 'The host application that features attach to.' },
  { value: 'feature', title: 'Feature Project', blurb: 'A feature built independently and mounted in a Master.' },
]

export function CreateProject() {
  const navigate = useNavigate()
  const { projects, createProject, refresh } = useProjects()
  const masters = projects.filter((p) => p.type === 'master')

  const [values, setValues] = useState<CreateProjectInput>({ name: '', type: 'master', initialPrompt: '', screenshots: [], masterId: '', basePath: '' })
  const [errors, setErrors] = useState<ProjectFormErrors>({})
  const [submitted, setSubmitted] = useState(false)
  const [saving, setSaving] = useState(false)
  const [submitError, setSubmitError] = useState<string>()
  const inFlight = useRef(false)
  const fileItems = useLocalFileItems(values.screenshots)

  // Pick up Masters created elsewhere or in an earlier session.
  useEffect(() => {
    refresh()
  }, [refresh])

  function update(patch: Partial<CreateProjectInput>) {
    const next = { ...values, ...patch }
    setValues(next)
    if (submitted) setErrors(validateProjectForm(next, projects))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setSubmitted(true)
    const found = validateProjectForm(values, projects)
    setErrors(found)
    if (Object.keys(found).length > 0 || inFlight.current) return
    inFlight.current = true
    setSaving(true)
    setSubmitError(undefined)
    try {
      const { project, uploadError } = await createProject(values)
      navigate(`/projects/${project.id}`, { state: uploadError ? { uploadError } : undefined })
    } catch (err) {
      if (err instanceof ApiError && Object.keys(err.fields).length > 0) {
        const { name, initialPrompt, masterId, basePath } = err.fields
        setErrors({ name, initialPrompt, masterId, basePath })
        setSubmitted(true)
      }
      setSubmitError(err instanceof Error ? err.message : 'Could not create the project.')
      inFlight.current = false
      setSaving(false)
    }
  }

  const isFeature = values.type === 'feature'

  return (
    <div className="page" style={{ maxWidth: 760 }}>
      <div className="page__head">
        <div>
          <h1>Create project</h1>
          <p>Define the project, then describe the UI you want in the builder.</p>
        </div>
      </div>

      <form className="form card" onSubmit={onSubmit} noValidate>
        <fieldset className="type-picker">
          <legend className="field__label" style={{ marginBottom: 8 }}>
            Project type
          </legend>
          {TYPE_OPTIONS.map((o) => (
            <label key={o.value} className="type-option">
              <input type="radio" name="type" value={o.value} checked={values.type === o.value} onChange={() => update({ type: o.value })} />
              <div>
                <strong>{o.title}</strong>
                <span>{o.blurb}</span>
              </div>
            </label>
          ))}
        </fieldset>

        <Field label="Project name" error={errors.name}>
          {(a) => <input {...a} className="input" value={values.name} maxLength={80} onChange={(e) => update({ name: e.target.value })} />}
        </Field>

        {isFeature && (
          <>
            <Field label="Master Project" error={errors.masterId} hint={masters.length === 0 ? 'No Master Projects exist yet. Create a Master Project first.' : undefined}>
              {(a) => (
                <select {...a} className="select" value={values.masterId} disabled={masters.length === 0} onChange={(e) => update({ masterId: e.target.value })}>
                  <option value="">Select a Master Project…</option>
                  {masters.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field label="Base path" error={errors.basePath} hint={normalizeBasePath(values.basePath ?? '') ? `Will be stored as ${normalizeBasePath(values.basePath ?? '')} (lowercased, no trailing slash).` : 'Where the feature mounts inside the Master, e.g. /accounts.'}>
              {(a) => <input {...a} className="input" placeholder="/accounts" value={values.basePath} onChange={(e) => update({ basePath: e.target.value })} />}
            </Field>
          </>
        )}

        <Field label="Initial prompt" error={errors.initialPrompt} hint="Optional if you attach screenshots.">
          {(a) => (
            <textarea {...a} className="textarea" placeholder="Describe the UI you want to build…" value={values.initialPrompt} onChange={(e) => update({ initialPrompt: e.target.value })} />
          )}
        </Field>

        <div className="panel-section">
          <span className="field__label">Screenshots</span>
          <ScreenshotUploader
            items={fileItems}
            busy={saving}
            onAdd={(added) => update({ screenshots: [...values.screenshots, ...added] })}
            onRemove={(i) => update({ screenshots: values.screenshots.filter((_, j) => j !== i) })}
            onReplace={(i, f) => update({ screenshots: values.screenshots.map((old, j) => (j === i ? f : old)) })}
          />
        </div>

        {submitError && <Banner tone="danger" title="Couldn't create project">{submitError}</Banner>}

        <div className="form__actions">
          <LinkButton to="/">Cancel</LinkButton>
          <Button type="submit" variant="primary" disabled={saving}>
            {saving ? 'Creating…' : 'Create project'}
          </Button>
        </div>
      </form>
    </div>
  )
}

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { App } from './App'
import { createFakeProjectsApi } from './test/fakeProjectsApi'
import { ProjectsProvider } from './state/ProjectsContext'

// These tests exercise the UI against a TEST DOUBLE of the API. They do not prove model inference or sandbox builds.
async function open(prompt = 'A dashboard') {
  const api = createFakeProjectsApi()
  const { project } = await api.createProject({ name: 'Demo', type: 'master', initialPrompt: prompt, screenshots: [] })
  render(
    <MemoryRouter initialEntries={[`/projects/${project.id}`]}>
      <ProjectsProvider api={api}>
        <App />
      </ProjectsProvider>
    </MemoryRouter>,
  )
  await screen.findByRole('heading', { name: 'Demo' })
  return { api, project, user: userEvent.setup() }
}

describe('generation flow', () => {
  it('generates from a prompt only and shows a sandboxed preview of the real version', async () => {
    const { api, user } = await open()
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    const frame = await screen.findByTitle('Preview of version 1')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
    expect(api.started[0].input.screenshotIds).toEqual([])
    expect(api.started[0].input.prompt).toBe('A dashboard')
  })

  it('generates from a screenshot only (empty prompt)', async () => {
    const { api, user } = await open()
    URL.createObjectURL = () => 'blob:x'
    URL.revokeObjectURL = () => {}
    await user.clear(screen.getByLabelText('Prompt'))
    await user.upload(screen.getByLabelText('Upload screenshots'), new File(['x'], 'a.png', { type: 'image/png' }))
    await screen.findByAltText('Preview of a.png')
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    await screen.findByTitle('Preview of version 1')
    expect(api.started[0].input.prompt).toBeUndefined()
    expect(api.started[0].input.screenshotIds).toHaveLength(1)
  })

  it('sends both prompt and screenshot together', async () => {
    const { api, user } = await open('Make it blue')
    URL.createObjectURL = () => 'blob:x'
    URL.revokeObjectURL = () => {}
    await user.upload(screen.getByLabelText('Upload screenshots'), new File(['x'], 'a.png', { type: 'image/png' }))
    await screen.findByAltText('Preview of a.png')
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    await screen.findByTitle('Preview of version 1')
    expect(api.started[0].input.prompt).toBe('Make it blue')
    expect(api.started[0].input.screenshotIds).toHaveLength(1)
  })

  it('refuses to generate with neither prompt nor screenshot', async () => {
    const { api, user } = await open()
    await user.clear(screen.getByLabelText('Prompt'))
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    expect(screen.getByText(/Add a prompt, or at least one screenshot/)).toBeInTheDocument()
    expect(api.started).toHaveLength(0)
  })

  it('shows an actionable config error and disables generation when AI is not configured', async () => {
    const api = createFakeProjectsApi()
    api.readiness = { ...api.readiness, aiConfigured: false, ready: false }
    const { project } = await api.createProject({ name: 'Demo', type: 'master', initialPrompt: 'x', screenshots: [] })
    render(
      <MemoryRouter initialEntries={[`/projects/${project.id}`]}>
        <ProjectsProvider api={api}>
          <App />
        </ProjectsProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByText('Generation is not available')).toBeInTheDocument()
    expect(screen.getByText(/AI_API_KEY/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate UI' })).toBeDisabled()
  })

  it('shows a failure with its error code and no preview, never a fake success', async () => {
    const { api, user } = await open()
    api.failNext = { code: 'build_failed', message: 'The generated project did not build.', retryable: true }
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    expect(await screen.findByText('The generated project did not build.')).toBeInTheDocument()
    expect(screen.getByText('No preview available')).toBeInTheDocument()
    expect(screen.queryByTitle(/Preview of version/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show build log' })).toBeInTheDocument()
  })

  it('refines the selected version, creating a new version with a parent reference', async () => {
    const { api, user } = await open()
    // One composer: before any version it generates; once a version exists the same button refines it.
    expect(screen.queryByRole('button', { name: 'Apply changes' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    await screen.findByTitle('Preview of version 1')
    await user.type(screen.getByLabelText('Additional prompt'), 'Use a blue theme')
    await user.click(screen.getByRole('button', { name: 'Apply changes' }))
    await screen.findByTitle('Preview of version 2')
    // Both messages stay in the thread, each followed by the version it produced.
    expect(screen.getByText('A dashboard')).toBeInTheDocument()
    expect(screen.getByText('Use a blue theme')).toBeInTheDocument()
    expect(screen.getByText('Version 1')).toBeInTheDocument()
    expect(screen.getByText('Version 2')).toBeInTheDocument()
    expect(api.started[1].input.parentVersionId).toBe(api.versions[0].id)
    expect(api.versions[1].parentVersionId).toBe(api.versions[0].id)
  })

  it('keeps the previous version visible when a refinement fails', async () => {
    const { api, user } = await open()
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    await screen.findByTitle('Preview of version 1')
    api.failNext = { code: 'ai_unavailable', message: 'The AI provider is unavailable.', retryable: true }
    await user.type(screen.getByLabelText('Additional prompt'), 'Change it')
    await user.click(screen.getByRole('button', { name: 'Apply changes' }))
    expect(await screen.findByText('The AI provider is unavailable.')).toBeInTheDocument()
    expect(screen.getByTitle('Preview of version 1')).toBeInTheDocument()
  })

  it('restores versions after a reload (new render with the same backend state)', async () => {
    const { api, project, user } = await open()
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    await screen.findByTitle('Preview of version 1')
    document.body.innerHTML = ''
    render(
      <MemoryRouter initialEntries={[`/projects/${project.id}`]}>
        <ProjectsProvider api={api}>
          <App />
        </ProjectsProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByTitle('Preview of version 1')).toBeInTheDocument()
  })

  it('offers the source of each version as a download instead of showing it', async () => {
    const { api, user } = await open()
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    await screen.findByTitle('Preview of version 1')
    const link = screen.getByRole('link', { name: 'Download source' })
    expect(link).toHaveAttribute('href', api.downloadUrl(api.versions[0]))
    expect(link).toHaveAttribute('download')
    expect(screen.queryByRole('button', { name: 'src/App.tsx' })).not.toBeInTheDocument()
  })

  it('hides Retry for a non-retryable failure and tells the user to adjust the request', async () => {
    const { api, user } = await open()
    api.failNext = { code: 'invalid_artifact', message: 'The model output did not pass validation.', retryable: false }
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    expect(await screen.findByText('The model output did not pass validation.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.getByText(/Adjust the prompt or screenshots/)).toBeInTheDocument()
  })

  it('hides Retry once the attempt limit reported by the backend is reached', async () => {
    const api = createFakeProjectsApi()
    const { project } = await api.createProject({ name: 'Demo', type: 'master', initialPrompt: 'x', screenshots: [] })
    api.failNext = { code: 'ai_unavailable', message: 'The AI provider is unavailable.', retryable: true }
    await api.startGeneration(project.id, { prompt: 'x', screenshotIds: [] })
    api.generations[0].attempts = api.readiness.maxSessionAttempts
    render(
      <MemoryRouter initialEntries={[`/projects/${project.id}`]}>
        <ProjectsProvider api={api}>
          <App />
        </ProjectsProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByText('The AI provider is unavailable.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('does not claim success while a session is still running', async () => {
    const api = createFakeProjectsApi()
    const { project } = await api.createProject({ name: 'Demo', type: 'master', initialPrompt: 'x', screenshots: [] })
    const g = await api.startGeneration(project.id, { prompt: 'x', screenshotIds: [] })
    api.generations[0] = { ...g, status: 'building', versionId: null }
    api.versions.length = 0
    render(
      <MemoryRouter initialEntries={[`/projects/${project.id}`]}>
        <ProjectsProvider api={api}>
          <App />
        </ProjectsProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByText('Building in the isolated sandbox', { selector: 'h2' })).toBeInTheDocument()
    expect(screen.queryByTitle(/Preview of version/)).not.toBeInTheDocument()
    expect(screen.queryByText('Completed')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate UI' })).toBeDisabled()
  })
})

describe('delete project', () => {
  it('asks for confirmation, deletes on the server, and removes the card', async () => {
    const api = createFakeProjectsApi()
    await api.createProject({ name: 'Demo', type: 'master', initialPrompt: 'x', screenshots: [] })
    render(
      <MemoryRouter initialEntries={['/']}>
        <ProjectsProvider api={api}>
          <App />
        </ProjectsProvider>
      </MemoryRouter>,
    )
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Actions for Demo' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'No' })) // No closes the popup and keeps the project
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(await api.listProjects()).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Actions for Demo' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    expect(await api.listProjects()).toHaveLength(1) // nothing happens before the confirmation
    await user.click(screen.getByRole('button', { name: 'Yes' }))
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Demo' })).not.toBeInTheDocument())
    expect(await api.listProjects()).toHaveLength(0)
  })

  it('keeps the card and shows the reason when the server refuses', async () => {
    const api = createFakeProjectsApi()
    const { project: m } = await api.createProject({ name: 'Host', type: 'master', initialPrompt: 'x', screenshots: [] })
    await api.createProject({ name: 'Login', type: 'feature', masterId: m.id, basePath: '/accounts', initialPrompt: 'x', screenshots: [] })
    render(
      <MemoryRouter initialEntries={['/']}>
        <ProjectsProvider api={api}>
          <App />
        </ProjectsProvider>
      </MemoryRouter>,
    )
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Actions for Host' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Yes' }))
    expect(await screen.findByText(/still has Feature Projects/)).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'Host' })).toBeInTheDocument()
  })
})

describe('project card', () => {
  it('opens the project when the card itself is clicked', async () => {
    const api = createFakeProjectsApi()
    const { project } = await api.createProject({ name: 'Demo', type: 'master', initialPrompt: 'x', screenshots: [] })
    render(
      <MemoryRouter initialEntries={['/']}>
        <ProjectsProvider api={api}>
          <App />
        </ProjectsProvider>
      </MemoryRouter>,
    )
    const user = userEvent.setup()
    await user.click(await screen.findByText('Modified'))
    expect(await screen.findByRole('heading', { name: 'Demo' })).toBeInTheDocument()
    expect(await screen.findByLabelText('Prompt')).toBeInTheDocument()
    void project
  })
})

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { App } from './App'
import { ApiError, type ProjectsApi } from './api/projectsApi'
import { createFakeProjectsApi } from './test/fakeProjectsApi'
import { ProjectsProvider } from './state/ProjectsContext'

function setup(route = '/', api: ProjectsApi = createFakeProjectsApi()) {
  render(
    <MemoryRouter initialEntries={[route]}>
      <ProjectsProvider api={api}>
        <App />
      </ProjectsProvider>
    </MemoryRouter>,
  )
  return userEvent.setup()
}

describe('app flow', () => {
  it('shows the empty dashboard with a prototype-data warning', async () => {
    setup()
    expect(await screen.findByText('No projects yet')).toBeInTheDocument()
    expect(screen.getByText(/not saved/i)).toBeInTheDocument()
  })

  it('shows validation errors and does not navigate on an empty form', async () => {
    const user = setup('/projects/new')
    await user.click(screen.getByRole('button', { name: 'Create project' }))
    expect(await screen.findByText('Enter a project name.')).toBeInTheDocument()
    expect(screen.getByText(/Describe the UI you want/)).toBeInTheDocument()
  })

  it('creates a master project, opens the builder, then lists it on the dashboard', async () => {
    const user = setup('/projects/new')
    await user.type(screen.getByLabelText('Project name'), 'Core Banking')
    await user.type(screen.getByLabelText('Initial prompt'), 'An accounts overview')
    await user.click(screen.getByRole('button', { name: 'Create project' }))

    expect(await screen.findByRole('heading', { name: 'Core Banking' })).toBeInTheDocument()
    await user.clear(screen.getByLabelText('Prompt'))
    await user.click(screen.getByRole('button', { name: 'Generate UI' }))
    expect(screen.getByText(/Add a prompt, or at least one screenshot/)).toBeInTheDocument()
    expect(screen.getByText('Not published')).toBeInTheDocument()

    await user.click(screen.getByRole('link', { name: /Projects/ }))
    const card = await screen.findByRole('article', { name: 'Core Banking' })
    expect(within(card).getByRole('link', { name: 'Continue' })).toBeInTheDocument()
  })

  it('requires master and base path for feature projects', async () => {
    const user = setup('/projects/new')
    await user.click(screen.getByLabelText(/Feature Project/))
    await user.type(screen.getByLabelText('Project name'), 'Billing')
    await user.type(screen.getByLabelText('Initial prompt'), 'Invoices')
    await user.click(screen.getByRole('button', { name: 'Create project' }))
    expect(await screen.findByText(/Select the Master Project/)).toBeInTheDocument()
    expect(screen.getByText(/Enter the base path/)).toBeInTheDocument()
  })

  it('creates a feature under a master and opens the builder with master and base path', async () => {
    const user = setup('/projects/new')
    await user.type(screen.getByLabelText('Project name'), 'Core Banking')
    await user.type(screen.getByLabelText('Initial prompt'), 'Shell')
    await user.click(screen.getByRole('button', { name: 'Create project' }))
    await screen.findByRole('heading', { name: 'Core Banking' })
    await user.click(screen.getByRole('link', { name: /Projects/ }))
    await user.click(await screen.findByRole('link', { name: '+ Create project' }))

    await user.click(screen.getByLabelText(/Feature Project/))
    await user.type(screen.getByLabelText('Project name'), 'Accounts')
    await user.selectOptions(screen.getByLabelText('Master Project'), 'Core Banking')
    await user.type(screen.getByLabelText('Base path'), 'Accounts/')
    expect(screen.getByText(/Will be stored as \/accounts/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Initial prompt'), 'Account list')
    await user.click(screen.getByRole('button', { name: 'Create project' }))

    expect(await screen.findByRole('heading', { name: 'Accounts' })).toBeInTheDocument()
    expect(screen.getByText('Feature')).toBeInTheDocument()
    expect(screen.getByText(/Feature of/)).toHaveTextContent('Core Banking')
    expect(screen.getByText(/mounts at/)).toHaveTextContent('/accounts')

    // duplicate path under the same Master is rejected
    await user.click(screen.getByRole('link', { name: /Projects/ }))
    await user.click(await screen.findByRole('link', { name: '+ Create project' }))
    await user.click(screen.getByLabelText(/Feature Project/))
    await user.type(screen.getByLabelText('Project name'), 'Accounts 2')
    await user.selectOptions(screen.getByLabelText('Master Project'), 'Core Banking')
    await user.type(screen.getByLabelText('Base path'), '/ACCOUNTS')
    await user.type(screen.getByLabelText('Initial prompt'), 'x')
    await user.click(screen.getByRole('button', { name: 'Create project' }))
    expect(await screen.findByText(/already uses "\/accounts"/)).toBeInTheDocument()
  })

  it('attaches and removes screenshots', async () => {
    const user = setup('/projects/new')
    const file = new File(['x'], 'home.png', { type: 'image/png' })
    URL.createObjectURL = () => 'blob:x'
    URL.revokeObjectURL = () => {}
    await user.upload(screen.getByLabelText('Upload screenshots'), file)
    expect(await screen.findByAltText('Preview of home.png')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remove home.png' }))
    expect(screen.queryByAltText('Preview of home.png')).not.toBeInTheDocument()
  })

  it('shows a not-found state for an unknown project id', async () => {
    setup('/projects/00000000-0000-4000-8000-000000000000')
    expect(await screen.findByText('Project not found')).toBeInTheDocument()
  })

  it('loads an existing project by id and restores its context and screenshots', async () => {
    const api = createFakeProjectsApi()
    const master = (await api.createProject({ name: 'Core Banking', type: 'master', initialPrompt: 'Shell', screenshots: [] })).project
    const feature = (
      await api.createProject({
        name: 'Accounts',
        type: 'feature',
        initialPrompt: 'List',
        masterId: master.id,
        basePath: '/accounts',
        screenshots: [new File(['x'], 'home.png', { type: 'image/png' })],
      })
    ).project
    setup(`/projects/${feature.id}`, api)
    expect(await screen.findByRole('heading', { name: 'Accounts' })).toBeInTheDocument()
    expect(screen.getByText(/Feature of/)).toHaveTextContent('Core Banking')
    expect(screen.getByText(/mounts at/)).toHaveTextContent('/accounts')
    expect(screen.getByDisplayValue('List')).toBeInTheDocument()
    expect(screen.getByAltText('Preview of home.png')).toBeInTheDocument()
  })

  it('saves added screenshots and deletes removed ones through the API', async () => {
    const api = createFakeProjectsApi()
    const { project } = await api.createProject({ name: 'Core Banking', type: 'master', initialPrompt: 'Shell', screenshots: [] })
    const user = setup(`/projects/${project.id}`, api)
    await screen.findByRole('heading', { name: 'Core Banking' })
    await user.upload(screen.getByLabelText('Upload screenshots'), new File(['x'], 'a.png', { type: 'image/png' }))
    expect(await screen.findByAltText('Preview of a.png')).toBeInTheDocument()
    expect(api.projects[0].screenshots).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Remove a.png' }))
    await waitFor(() => expect(screen.queryByAltText('Preview of a.png')).not.toBeInTheDocument())
    expect(api.projects[0].screenshots).toHaveLength(0)
  })

  it('shows an error and keeps the list unchanged when a screenshot upload fails', async () => {
    const api = createFakeProjectsApi()
    const { project } = await api.createProject({ name: 'Core Banking', type: 'master', initialPrompt: 'Shell', screenshots: [] })
    api.uploadScreenshots = async () => {
      throw new ApiError(415, 'unsupported_file_type', 'not a supported image')
    }
    const user = setup(`/projects/${project.id}`, api)
    await screen.findByRole('heading', { name: 'Core Banking' })
    await user.upload(screen.getByLabelText('Upload screenshots'), new File(['x'], 'a.png', { type: 'image/png' }))
    expect(await screen.findByText(/not a supported image/)).toBeInTheDocument()
    expect(screen.queryByAltText('Preview of a.png')).not.toBeInTheDocument()
  })

  it('shows server field errors on the form and stays on the page', async () => {
    const api = createFakeProjectsApi()
    api.createProject = async () => {
      throw new ApiError(409, 'base_path_conflict', 'Conflict', { basePath: 'Another feature under this Master already uses "/x".' })
    }
    const user = setup('/projects/new', api)
    await user.type(screen.getByLabelText('Project name'), 'Billing')
    await user.type(screen.getByLabelText('Initial prompt'), 'x')
    await user.click(screen.getByRole('button', { name: 'Create project' }))
    expect(await screen.findByRole('button', { name: 'Create project' })).toBeEnabled()
    expect(screen.getByText('Conflict')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Create project' })).toBeInTheDocument()
  })

  it('reports an unreachable backend on the dashboard instead of showing an empty list', async () => {
    const api = createFakeProjectsApi()
    api.listProjects = async () => {
      throw new ApiError(0, 'network_error', "Can't reach the backend API")
    }
    setup('/', api)
    expect(await screen.findByText("Couldn't load projects")).toBeInTheDocument()
    expect(screen.getByText(/Can't reach the backend API/)).toBeInTheDocument()
    expect(screen.queryByText('No projects yet')).not.toBeInTheDocument()
  })

  it('opens the builder with an upload warning when screenshots failed after the project was created', async () => {
    const api = createFakeProjectsApi()
    const original = api.createProject
    api.createProject = async (input) => {
      const result = await original({ ...input, screenshots: [] })
      return { ...result, uploadError: 'file too large' }
    }
    const user = setup('/projects/new', api)
    await user.type(screen.getByLabelText('Project name'), 'Core Banking')
    await user.type(screen.getByLabelText('Initial prompt'), 'Shell')
    await user.click(screen.getByRole('button', { name: 'Create project' }))
    expect(await screen.findByRole('heading', { name: 'Core Banking' })).toBeInTheDocument()
    expect(screen.getByText(/screenshots were not uploaded: file too large/)).toBeInTheDocument()
  })
})

import { createHttpProjectsApi } from './httpProjectsApi'
import { ApiError } from './projectsApi'

const BASE = 'http://api.test/api'

function jsonResponse(status: number, body?: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

const detail = { id: 'p1', name: 'Core', type: 'master', initialPrompt: '', screenshotCount: 0, status: 'draft', createdAt: '', updatedAt: '', screenshots: [] }

describe('http projects api', () => {
  it('lists projects from the configured base URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, [detail]))
    const api = createHttpProjectsApi(BASE + '/', fetchMock)
    expect(await api.listProjects()).toHaveLength(1)
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/projects`)
    expect(api.persistence).toBe('server')
  })

  it('posts camelCase JSON and sends masterId/basePath only for features', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(201, detail))
    const api = createHttpProjectsApi(BASE, fetchMock)
    await api.createProject({ name: 'Core', type: 'master', initialPrompt: 'p', screenshots: [], masterId: 'ignored', basePath: '/ignored' })
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ name: 'Core', type: 'master', initialPrompt: 'p' })
    await api.createProject({ name: 'Acc', type: 'feature', initialPrompt: 'p', screenshots: [], masterId: 'm1', basePath: '/acc' })
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({ type: 'feature', masterId: 'm1', basePath: '/acc' })
  })

  it('maps server error bodies to ApiError with field messages', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(409, { error: { code: 'base_path_conflict', message: 'dup', fields: { basePath: 'dup' } } }))
    const api = createHttpProjectsApi(BASE, fetchMock)
    const err = await api.createProject({ name: 'Acc', type: 'feature', initialPrompt: '', screenshots: [], masterId: 'm', basePath: '/a' }).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 409, code: 'base_path_conflict', fields: { basePath: 'dup' } })
  })

  it('reports a network failure as an ApiError with a clear message', async () => {
    const api = createHttpProjectsApi(BASE, vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const err = await api.listProjects().catch((e) => e)
    expect(err).toMatchObject({ status: 0, code: 'network_error' })
    expect(err.message).toContain(BASE)
  })

  it('keeps the created project and reports upload failure separately', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(201, detail))
      .mockResolvedValueOnce(jsonResponse(415, { error: { code: 'unsupported_file_type', message: 'bad image', fields: {} } }))
    const api = createHttpProjectsApi(BASE, fetchMock)
    const result = await api.createProject({ name: 'Core', type: 'master', initialPrompt: 'p', screenshots: [new File(['x'], 'a.png', { type: 'image/png' })] })
    expect(result.project.id).toBe('p1')
    expect(result.uploadError).toBe('bad image')
  })

  it('uploads files as multipart and resolves screenshot URLs against the API origin', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(201, []))
    const api = createHttpProjectsApi(BASE, fetchMock)
    await api.uploadScreenshots('p1', [new File(['x'], 'a.png', { type: 'image/png' })])
    expect(fetchMock.mock.calls[0][1].body).toBeInstanceOf(FormData)
    expect(fetchMock.mock.calls[0][1].headers).toBeUndefined() // browser sets the multipart boundary
    expect(api.screenshotSrc({ url: '/api/screenshots/s1' } as never)).toBe('http://api.test/api/screenshots/s1')
  })

  it('treats 204 as success when deleting', async () => {
    const api = createHttpProjectsApi(BASE, vi.fn().mockResolvedValue(new Response(null, { status: 204 })))
    await expect(api.deleteScreenshot('s1')).resolves.toBeUndefined()
  })

  it('starts a generation with only the fields provided (prompt optional)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(202, { id: 'g1' }))
    const api = createHttpProjectsApi(BASE, fetchMock)
    await api.startGeneration('p1', { screenshotIds: ['s1'] })
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/projects/p1/generations`)
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ screenshotIds: ['s1'] })
    await api.startGeneration('p1', { prompt: '  hi ', screenshotIds: [], parentVersionId: 'v1' })
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ prompt: 'hi', screenshotIds: [], parentVersionId: 'v1' })
  })

  it('reads a version source file as text and encodes each path segment', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('export {}', { status: 200 }))
    const api = createHttpProjectsApi(BASE, fetchMock)
    expect(await api.getVersionFile('v1', 'src/a b.tsx')).toBe('export {}')
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/versions/v1/files/src/a%20b.tsx`)
  })

  it('builds the preview URL on the API origin and maps provider errors', async () => {
    const api = createHttpProjectsApi(BASE, vi.fn().mockResolvedValue(jsonResponse(503, { error: { code: 'ai_not_configured', message: 'No token' } })))
    expect(api.previewSrc({ previewUrl: '/api/previews/v1/' } as never)).toBe('http://api.test/api/previews/v1/')
    await expect(api.startGeneration('p1', { prompt: 'x', screenshotIds: [] })).rejects.toMatchObject({ status: 503, code: 'ai_not_configured' })
  })
})

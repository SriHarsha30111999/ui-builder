import { normalizeBasePath, validateBasePath, validateProjectForm, validateScreenshot } from './validation'
import type { Project } from './types/project'
import type { CreateProjectInput } from './types/project'

const base: CreateProjectInput = { name: 'Billing', type: 'master', initialPrompt: 'A dashboard', screenshots: [] }
const img = (name: string, type = 'image/png', size = 100) => new File([new Uint8Array(size)], name, { type })

describe('validateProjectForm', () => {
  it('accepts a valid master project', () => {
    expect(validateProjectForm(base)).toEqual({})
  })
  it('requires a name of at least 3 characters', () => {
    expect(validateProjectForm({ ...base, name: ' ' }).name).toMatch(/Enter a project name/)
    expect(validateProjectForm({ ...base, name: 'ab' }).name).toMatch(/at least 3/)
  })
  it('requires a prompt or a screenshot', () => {
    expect(validateProjectForm({ ...base, initialPrompt: '' }).initialPrompt).toBeDefined()
    expect(validateProjectForm({ ...base, initialPrompt: '', screenshots: [img('a.png')] })).toEqual({})
  })
  it('requires master and valid base path for feature projects', () => {
    const f: CreateProjectInput = { ...base, type: 'feature', masterId: '', basePath: '' }
    expect(validateProjectForm(f)).toMatchObject({ masterId: expect.any(String), basePath: expect.any(String) })
    expect(validateProjectForm({ ...f, masterId: 'm1', basePath: 'billing' })).toEqual({})
    expect(validateProjectForm({ ...f, masterId: 'm1', basePath: '/a b' }).basePath).toMatch(/spaces/)
    expect(validateProjectForm({ ...f, masterId: 'm1', basePath: '/billing' })).toEqual({})
  })
})

describe('validateScreenshot', () => {
  it('rejects non-images and oversized files', () => {
    expect(validateScreenshot(img('a.pdf', 'application/pdf'))).toMatch(/not a supported/)
    expect(validateScreenshot(img('big.png', 'image/png', 11 * 1024 * 1024))).toMatch(/10 MB/)
    expect(validateScreenshot(img('ok.png'))).toBeUndefined()
  })
})

const feat = (masterId: string, basePath: string) => ({ type: 'feature', masterId, basePath }) as Project

describe('base path', () => {
  it('normalizes slashes and case', () => {
    expect(normalizeBasePath('accounts')).toBe('/accounts')
    expect(normalizeBasePath(' //Accounts// ')).toBe('/accounts')
    expect(normalizeBasePath('/a//b/')).toBe('/a/b')
    expect(normalizeBasePath('/')).toBe('')
  })
  it('rejects empty, root and invalid formats', () => {
    expect(validateBasePath('', 'm1', [])).toBeDefined()
    expect(validateBasePath('/', 'm1', [])).toBeDefined()
    expect(validateBasePath('/a?b=1', 'm1', [])).toBeDefined()
    expect(validateBasePath('/../etc', 'm1', [])).toBeDefined()
    expect(validateBasePath('/-bad', 'm1', [])).toBeDefined()
    expect(validateBasePath('/ok-path/sub_1', 'm1', [])).toBeUndefined()
  })
  it('rejects reserved Master routes', () => {
    expect(validateBasePath('/Login/', 'm1', [])).toMatch(/reserved/)
    expect(validateBasePath('/api/v1', 'm1', [])).toMatch(/reserved/)
  })
  it('rejects duplicates within a Master only, after normalization', () => {
    const existing = [feat('m1', '/accounts')]
    expect(validateBasePath('Accounts/', 'm1', existing)).toMatch(/already uses/)
    expect(validateBasePath('/accounts', 'm2', existing)).toBeUndefined()
  })
})

describe('base path rules shared with the backend', () => {
  it('rejects percent signs and over-long paths', () => {
    expect(validateBasePath('/a%20b', 'm', [])).toMatch(/cannot contain/)
    expect(validateBasePath('/' + 'a'.repeat(120), 'm', [])).toMatch(/100 characters/)
  })
  it('trims surrounding whitespace before checking characters', () => {
    expect(validateBasePath('  /Reports/ ', 'm', [])).toBeUndefined()
  })
})

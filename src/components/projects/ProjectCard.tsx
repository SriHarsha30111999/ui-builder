import { useEffect, useRef, useState, type MouseEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Project } from '../../types/project'
import { formatRelative } from '../../lib'
import { Button, LinkButton } from '../ui/primitives'
import { StatusBadge, TypeBadge } from './badges'

export function ProjectCard({ project, masterName, onDelete }: { project: Project; masterName?: string; onDelete?: () => Promise<void> }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string>()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!menuOpen) return
    const close = (e: Event) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !menuRef.current?.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', close)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', close)
    }
  }, [menuOpen])

  // The whole card opens the project; real controls inside it (Open, Delete, the confirmation) keep their own behaviour.
  function openFromCard(e: MouseEvent) {
    if ((e.target as HTMLElement).closest('a, button, [role="alertdialog"]')) return
    navigate(`/projects/${project.id}`)
  }

  async function remove() {
    setDeleting(true)
    setError(undefined)
    try {
      await onDelete?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The project could not be deleted.')
      setDeleting(false)
      setConfirming(false)
    }
  }

  return (
    <article className="project-card project-card--clickable" aria-label={project.name} onClick={openFromCard}>
      <div className="project-card__top">
        <h3 className="project-card__name">{project.name}</h3>
        <div className="project-card__top-right">
          <TypeBadge type={project.type} />
          {onDelete && (
            <div className="kebab" ref={menuRef}>
              <button
                type="button"
                className="kebab__btn"
                aria-label={`Actions for ${project.name}`}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((o) => !o)}
              >
                ⋮
              </button>
              {menuOpen && (
                <div className="kebab__menu" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    className="kebab__item kebab__item--danger"
                    onClick={() => {
                      setMenuOpen(false)
                      setConfirming(true)
                    }}
                  >
                    Delete
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      <dl className="project-card__meta">
        <dt>Status</dt>
        <dd>
          <StatusBadge status={project.status} />
        </dd>
        {project.type === 'feature' && (
          <>
            <dt>Master</dt>
            <dd>{masterName ?? 'Unknown Master'}</dd>
            <dt>Base path</dt>
            <dd>{project.basePath}</dd>
          </>
        )}
        <dt>Modified</dt>
        <dd>{formatRelative(project.updatedAt)}</dd>
      </dl>
      {error && (
        <span className="field__error" role="alert">
          {error}
        </span>
      )}
      {confirming && (
        <div className="modal-backdrop" onClick={(e) => e.stopPropagation()}>
          <div role="alertdialog" aria-modal="true" aria-label={`Delete ${project.name}`} className="modal">
            <h4 className="modal__title">Delete project?</h4>
            <p>Delete "{project.name}" and all its versions, screenshots and source? This cannot be undone.</p>
            <div className="modal__actions">
              <Button size="sm" disabled={deleting} onClick={() => setConfirming(false)}>
                No
              </Button>
              <Button size="sm" danger disabled={deleting} onClick={() => void remove()}>
                {deleting ? 'Deleting…' : 'Yes'}
              </Button>
            </div>
          </div>
        </div>
      )}
      <div className="project-card__foot">
        <span className="muted">{project.screenshotCount} screenshot(s)</span>
        <LinkButton to={`/projects/${project.id}`} variant="primary" size="sm">
          {project.status === 'draft' ? 'Continue' : 'Open'}
        </LinkButton>
      </div>
    </article>
  )
}

export function ProjectCardSkeleton() {
  return (
    <div className="project-card" aria-hidden="true">
      <div className="skeleton" style={{ height: 20, width: '60%' }} />
      <div className="skeleton" style={{ height: 14 }} />
      <div className="skeleton" style={{ height: 14, width: '80%' }} />
      <div className="skeleton" style={{ height: 28, width: 90, marginLeft: 'auto' }} />
    </div>
  )
}

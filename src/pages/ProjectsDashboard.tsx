import { useEffect, useState } from 'react'
import { ProjectCard, ProjectCardSkeleton } from '../components/projects/ProjectCard'
import { Banner, Button, LinkButton, SegmentedControl, StateBox } from '../components/ui/primitives'
import { useProjects } from '../state/ProjectsContext'
import type { Project, ProjectType } from '../types/project'

type PreviewState = 'live' | 'loading' | 'error'

function Section({ title, type, projects, all }: { title: string; type: ProjectType; projects: Project[]; all: Project[] }) {
  const { deleteProject } = useProjects()
  const masterName = (p: Project) => p.masterName ?? all.find((m) => m.id === p.masterId)?.name
  return (
    <section aria-labelledby={`sec-${type}`}>
      <div className="section-title">
        <h2 id={`sec-${type}`}>{title}</h2>
        <span className="muted">{projects.length}</span>
      </div>
      {projects.length === 0 ? (
        <StateBox title={`No ${type} projects yet`}>
          {type === 'master' ? 'A Master Project is the host application that Feature Projects plug into.' : 'Feature Projects are built independently and attached to a Master Project.'}
        </StateBox>
      ) : (
        <div className="project-grid">
          {projects.map((p) => (
            <ProjectCard key={p.id} project={p} masterName={masterName(p)} onDelete={() => deleteProject(p.id)} />
          ))}
        </div>
      )}
    </section>
  )
}

export function ProjectsDashboard() {
  const { projects, loadState, errorMessage, reload, refresh, persistence } = useProjects()
  const [override, setOverride] = useState<PreviewState>('live')

  // Always show what the server has now (e.g. after creating a project and coming back).
  useEffect(() => {
    refresh()
  }, [refresh])

  const effective = override === 'live' ? loadState : override
  const masters = projects.filter((p) => p.type === 'master')
  const features = projects.filter((p) => p.type === 'feature')

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <h1>Projects</h1>
          <p>Master Projects host the application; Feature Projects extend a Master.</p>
        </div>
        <LinkButton to="/projects/new" variant="primary">
          + Create project
        </LinkButton>
      </div>

      {persistence === 'session-only' && (
        <Banner tone="warning" title="Prototype data — not saved">
          Projects you create live in this browser tab's memory and disappear on reload.
        </Banner>
      )}

      <div className="status-row">
        <span className="muted">Layout review (UI states):</span>
        <SegmentedControl
          label="Dashboard state preview"
          value={override}
          onChange={setOverride}
          options={[
            { value: 'live', label: 'Actual' },
            { value: 'loading', label: 'Loading' },
            { value: 'error', label: 'Error' },
          ]}
        />
      </div>

      {effective === 'loading' && (
        <div className="project-grid" aria-busy="true" aria-label="Loading projects">
          <ProjectCardSkeleton />
          <ProjectCardSkeleton />
          <ProjectCardSkeleton />
        </div>
      )}

      {effective === 'error' && (
        <StateBox
          tone="danger"
          title="Couldn't load projects"
          action={
            <Button
              onClick={() => {
                setOverride('live')
                reload()
              }}
            >
              Try again
            </Button>
          }
        >
          {override === 'error' ? 'Example error layout (simulated for UI review).' : errorMessage}
        </StateBox>
      )}

      {effective === 'ready' &&
        (projects.length === 0 ? (
          <StateBox
            title="No projects yet"
            action={
              <LinkButton to="/projects/new" variant="primary">
                Create your first project
              </LinkButton>
            }
          >
            Start with a Master Project, then add Feature Projects that attach to it.
          </StateBox>
        ) : (
          <>
            <Section title="Master Projects" type="master" projects={masters} all={projects} />
            <Section title="Feature Projects" type="feature" projects={features} all={projects} />
          </>
        ))}
    </div>
  )
}

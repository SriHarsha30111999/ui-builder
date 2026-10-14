import { Link, Route, Routes } from 'react-router-dom'
import { Badge } from './components/ui/primitives'
import { CreateProject } from './pages/CreateProject'
import { ProjectsDashboard } from './pages/ProjectsDashboard'
import { UiBuilder } from './pages/UiBuilder'

export function App() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <Link to="/" className="app-header__brand">
          UI Builder
        </Link>
        <span className="app-header__spacer" />
        <Badge tone="warning">Phase 3 · AI generation</Badge>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<ProjectsDashboard />} />
          <Route path="/projects/new" element={<CreateProject />} />
          <Route path="/projects/:id" element={<UiBuilder />} />
          <Route path="*" element={<div className="page"><h1>Page not found</h1><Link to="/">Back to projects</Link></div>} />
        </Routes>
      </main>
    </div>
  )
}

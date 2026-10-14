import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import { createHttpProjectsApi, DEFAULT_API_BASE_URL } from './api/httpProjectsApi'
import { ProjectsProvider } from './state/ProjectsContext'
import './styles/tokens.css'
import './styles/global.css'

const api = createHttpProjectsApi(import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <ProjectsProvider api={api}>
        <App />
      </ProjectsProvider>
    </BrowserRouter>
  </StrictMode>,
)

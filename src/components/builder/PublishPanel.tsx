import { useState } from 'react'
import { Badge, Button, Field } from '../ui/primitives'
import { SOURCE_CONTROL_PROVIDERS, type SourceControlProvider } from '../../types/project'

/** Provider selection is local state only; export and publish are not implemented yet. */
export function PublishPanel() {
  const [provider, setProvider] = useState<SourceControlProvider>('GitHub')
  return (
    <section className="panel-section" aria-labelledby="publish-h">
      <h2 id="publish-h">Export &amp; publish</h2>
      <Field label="Source-control provider">
        {(a) => (
          <select {...a} className="select" value={provider} onChange={(e) => setProvider(e.target.value as SourceControlProvider)}>
            {SOURCE_CONTROL_PROVIDERS.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        )}
      </Field>
      <div className="status-row">
        <Button disabled title="Requires generated source (later phase)">
          Export source (.zip)
        </Button>
        <Button disabled title="Requires generated source and provider integration (later phase)">
          Publish to {provider}
        </Button>
      </div>
      <div className="status-row">
        <span className="muted">Publication</span>
        <Badge>Not published</Badge>
        <span className="muted">Connection</span>
        <Badge>Not connected</Badge>
      </div>
      <span className="field__hint">Provider integrations (including Bitbucket) are not implemented yet; this selector does not contact any service.</span>
    </section>
  )
}

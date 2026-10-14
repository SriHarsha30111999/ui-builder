import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react'
import { formatBytes } from '../../lib'
import { MAX_SCREENSHOTS, validateScreenshot } from '../../validation'
import { Button } from './primitives'

export interface UploaderItem {
  key: string
  name: string
  size: number
  previewUrl: string
}

interface Props {
  items: UploaderItem[]
  /** Called with files that passed client-side checks. May be async (e.g. server upload). */
  onAdd: (files: File[]) => void | Promise<void>
  onRemove: (index: number) => void | Promise<void>
  onReplace: (index: number, file: File) => void | Promise<void>
  /** Disables controls while a save is in flight. */
  busy?: boolean
}

/** Local (not yet uploaded) files as uploader items, with object-URL previews that are released on change/unmount. */
export function useLocalFileItems(files: File[]): UploaderItem[] {
  const urls = useMemo(() => files.map((f) => URL.createObjectURL(f)), [files])
  useEffect(() => () => urls.forEach((u) => URL.revokeObjectURL(u)), [urls])
  return files.map((f, i) => ({ key: `${f.name}-${f.lastModified}-${i}`, name: f.name, size: f.size, previewUrl: urls[i] }))
}

/** Item-based multi-image input with previews, remove and replace. Storage is up to the caller. */
export function ScreenshotUploader({ items, onAdd, onRemove, onReplace, busy = false }: Props) {
  const addRef = useRef<HTMLInputElement>(null)
  const replaceRef = useRef<HTMLInputElement>(null)
  const replaceIndex = useRef<number>(-1)
  const [issues, setIssues] = useState<string[]>([])
  const [dragging, setDragging] = useState(false)

  function accept(incoming: File[]) {
    if (busy) return
    const problems: string[] = []
    const valid: File[] = []
    for (const f of incoming) {
      const err = validateScreenshot(f)
      if (err) problems.push(err)
      else valid.push(f)
    }
    const room = MAX_SCREENSHOTS - items.length
    if (valid.length > room) {
      problems.push(`You can attach at most ${MAX_SCREENSHOTS} screenshots; ${Math.max(0, valid.length - room)} not added.`)
    }
    setIssues(problems)
    if (valid.length && room > 0) void onAdd(valid.slice(0, room))
  }

  function onDrop(e: DragEvent) {
    e.preventDefault()
    setDragging(false)
    accept(Array.from(e.dataTransfer.files))
  }

  return (
    <div className="panel-section">
      <div
        className={`dropzone${dragging ? ' dropzone--active' : ''}`}
        aria-busy={busy}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => !busy && addRef.current?.click()}
      >
        <strong aria-hidden="true" style={{ fontSize: 24, lineHeight: 1 }}>＋</strong>
        <strong>Drop screenshots here, or click to upload</strong>
        <span>PNG, JPEG, WebP or GIF · up to 10 MB each · max {MAX_SCREENSHOTS}</span>
        <span>Add several to show different states of the same UI (open dropdown, modal, expanded menu…).</span>
        <Button
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation()
            addRef.current?.click()
          }}
        >
          {busy ? 'Saving…' : 'Choose images…'}
        </Button>
        <input
          ref={addRef}
          type="file"
          accept="image/*"
          multiple
          className="sr-only"
          aria-label="Upload screenshots"
          disabled={busy}
          onChange={(e) => {
            accept(Array.from(e.target.files ?? []))
            e.target.value = ''
          }}
        />
      </div>

      <input
        ref={replaceRef}
        type="file"
        accept="image/*"
        className="sr-only"
        aria-label="Replace screenshot"
        disabled={busy}
        onChange={(e) => {
          const f = e.target.files?.[0]
          e.target.value = ''
          if (!f) return
          const err = validateScreenshot(f)
          setIssues(err ? [err] : [])
          if (!err) void onReplace(replaceIndex.current, f)
        }}
      />

      {issues.length > 0 && (
        <ul className="field__error" role="alert" style={{ margin: 0, paddingLeft: 18 }}>
          {issues.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}

      {items.length > 0 && (
        <ul className="thumbs" aria-label="Attached screenshots">
          {items.map((it, i) => (
            <li className="thumb" key={it.key}>
              <img src={it.previewUrl} alt={`Preview of ${it.name}`} />
              <div className="thumb__body">
                <div className="thumb__name" title={it.name}>
                  {it.name}
                </div>
                <div className="muted">{formatBytes(it.size)}</div>
                <div className="thumb__actions">
                  <Button
                    size="sm"
                    disabled={busy}
                    aria-label={`Replace ${it.name}`}
                    onClick={() => {
                      replaceIndex.current = i
                      replaceRef.current?.click()
                    }}
                  >
                    Replace
                  </Button>
                  <Button size="sm" danger disabled={busy} aria-label={`Remove ${it.name}`} onClick={() => void onRemove(i)}>
                    Remove
                  </Button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

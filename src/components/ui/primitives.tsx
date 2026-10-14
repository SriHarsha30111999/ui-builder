import { useEffect, useId, type ButtonHTMLAttributes, type ReactNode } from 'react'
import { Link, type LinkProps } from 'react-router-dom'

const cx = (...parts: (string | false | undefined)[]) => parts.filter(Boolean).join(' ')

/* ---------- Button ---------- */
type Variant = 'primary' | 'secondary' | 'ghost'
interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: 'sm' | 'md'
  danger?: boolean
}
const btnClass = (variant: Variant, size: 'sm' | 'md', danger?: boolean) =>
  cx('btn', variant === 'primary' && 'btn--primary', variant === 'ghost' && 'btn--ghost', size === 'sm' && 'btn--sm', danger && 'btn--danger')

export function Button({ variant = 'secondary', size = 'md', danger, className, type = 'button', ...rest }: ButtonProps) {
  return <button type={type} className={cx(btnClass(variant, size, danger), className)} {...rest} />
}

export function LinkButton({ variant = 'secondary', size = 'md', className, ...rest }: LinkProps & { variant?: Variant; size?: 'sm' | 'md' }) {
  return <Link className={cx(btnClass(variant, size), className)} {...rest} />
}

/* ---------- Badge ---------- */
export type Tone = 'neutral' | 'primary' | 'success' | 'warning' | 'danger' | 'info'
export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={cx('badge', tone !== 'neutral' && `badge--${tone}`)}>{children}</span>
}

/* ---------- Field ---------- */
interface FieldProps {
  label: string
  hint?: string
  error?: string
  /** Render-prop supplies ids/aria attributes to wire label, hint and error to the control. */
  children: (a: { id: string; 'aria-invalid': boolean; 'aria-describedby'?: string }) => ReactNode
}
export function Field({ label, hint, error, children }: FieldProps) {
  const id = useId()
  const describedBy = [hint && `${id}-hint`, error && `${id}-err`].filter(Boolean).join(' ') || undefined
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {children({ id, 'aria-invalid': !!error, 'aria-describedby': describedBy })}
      {hint && (
        <span id={`${id}-hint`} className="field__hint">
          {hint}
        </span>
      )}
      {error && (
        <span id={`${id}-err`} className="field__error" role="alert">
          {error}
        </span>
      )}
    </div>
  )
}

/* ---------- Banner ---------- */
export function Banner({ tone = 'info', title, children }: { tone?: 'info' | 'warning' | 'danger'; title?: string; children: ReactNode }) {
  return (
    <div className={cx('banner', `banner--${tone}`)} role={tone === 'danger' ? 'alert' : 'note'}>
      <div>
        {title && <div className="banner__title">{title}</div>}
        <div>{children}</div>
      </div>
    </div>
  )
}

/* ---------- Segmented control ---------- */
export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" className="segmented__option" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

/* ---------- Loading / empty / error ---------- */
export function Spinner({ label = 'Loading' }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />
}

export function StateBox({ tone, title, children, action }: { tone?: 'danger'; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className={cx('state-box', tone === 'danger' && 'state-box--danger')} role={tone === 'danger' ? 'alert' : undefined}>
      <h2>{title}</h2>
      {children && <p>{children}</p>}
      {action}
    </div>
  )
}

/* ---------- Dialog ---------- */
export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const titleId = useId()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} onClick={(e) => e.stopPropagation()}>
        <h2 id={titleId}>{title}</h2>
        <div className="dialog__body">{children}</div>
        <div className="form__actions">
          <Button variant="primary" autoFocus onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    </div>
  )
}

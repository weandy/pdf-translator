interface Props {
  title: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
}

export function ErrorCard({ title, message, actionLabel, onAction, secondaryLabel, onSecondary }: Props) {
  return (
    <section className="error-card" role="alert">
      <div className="error-card-icon" aria-hidden="true">
        !
      </div>
      <div className="error-card-body">
        <strong>{title}</strong>
        <p>{message}</p>
        {(actionLabel || secondaryLabel) && (
          <div className="error-card-actions">
            {actionLabel && onAction && (
              <button type="button" className="primary" onClick={onAction}>
                {actionLabel}
              </button>
            )}
            {secondaryLabel && onSecondary && (
              <button type="button" className="quiet danger" onClick={onSecondary}>
                {secondaryLabel}
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

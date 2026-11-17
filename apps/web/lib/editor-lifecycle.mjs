/** Nonmodal editors keep global logout reachable, even while a mutation is pending. */
export function attachEditorLifecycle(dialog, onClose, isBusy, events = window) {
  if (dialog && !dialog.open) dialog.show();
  const navigate = () => { if (!isBusy()) onClose(); };
  const escape = event => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    if (!isBusy()) onClose();
  };
  events.addEventListener('popstate', navigate);
  events.addEventListener('keydown', escape);
  return () => {
    events.removeEventListener('popstate', navigate);
    events.removeEventListener('keydown', escape);
  };
}

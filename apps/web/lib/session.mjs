/** Per-login browser lifetime. Secrets are private, memory-only and never identity keys. */
let sequence = 0;
export class SessionScope {
  #token; #timer; #notify; #controller = new AbortController();
  constructor(token, {expiresAt = null, onInvalidated = () => {}} = {}) {
    if (typeof token !== 'string' || !token) throw new Error('Missing session credential');
    this.id = ++sequence; this.expiresAt = expiresAt;
    this.#token = token; this.#notify = onInvalidated;
    if (expiresAt !== null && !Number.isFinite(Date.parse(expiresAt))) throw new Error('Invalid session expiry');
    if (expiresAt) this.#schedule();
  }
  get signal() { return this.#controller.signal; }
  get active() { return !this.signal.aborted; }
  #schedule() {
    const remaining = Date.parse(this.expiresAt) - Date.now();
    if (remaining <= 0) { this.invalidate('expired'); return; }
    this.#timer = setTimeout(() => this.#schedule(), Math.min(remaining, 2147483647));
    this.#timer.unref?.();
  }
  assertCurrent() {
    if (this.expiresAt && Date.parse(this.expiresAt) <= Date.now()) this.invalidate('expired');
    this.signal.throwIfAborted();
  }
  authorization() { this.assertCurrent(); return this.#token; }
  invalidate(reason = 'invalid') {
    if (!this.active) return;
    clearTimeout(this.#timer); this.#token = ''; this.#controller.abort(); this.#notify(reason);
  }
  toString() { return `session-${this.id}`; }
}
/** Also fences fetch implementations that ignore AbortSignal (late responses in tests/proxies). */
export function requestScope(credential, signal) {
  const session = credential instanceof SessionScope ? credential : null;
  session?.assertCurrent(); signal?.throwIfAborted();
  const combined = session ? (signal ? AbortSignal.any([session.signal, signal]) : session.signal) : signal;
  return {
    token: session ? session.authorization() : credential,
    signal: combined,
    assertCurrent() { combined?.throwIfAborted(); session?.assertCurrent(); },
    invalidate() { session?.invalidate('invalid'); },
  };
}

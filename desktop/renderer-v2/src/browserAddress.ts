/** Resolve what the user typed before handing a web URL to the native browser. */
export function browserAddress(input: string): URL | undefined {
  const value = input.trim();
  if (!value || /[\\\s]/.test(value) || /^[/?#]/.test(value)) return;
  const local = /^(?:localhost|127(?:\.\d+){3}|\[::1\])(?=[:/?#]|$)/i.test(value);
  const withPort = /^(?:[\w.-]+|\[[\da-f:]+\]):\d+(?=[/?#]|$)/i.test(value);
  const explicit = /^[a-z][\w+.-]*:/i.test(value) && !withPort;
  if (explicit && !/^https?:\/\//i.test(value)) return;
  try {
    const url = new URL(explicit ? value : (local || withPort ? "http://" : "https://") + value);
    if (/^https?:$/.test(url.protocol) && !url.username && !url.password) return url;
  } catch { /* Invalid port or host. Keep the address available for editing. */ }
}

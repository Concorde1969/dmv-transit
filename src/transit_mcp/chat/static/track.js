// Page activity for the site's own usage log (/api/events): which trip options people
// open, which panels they use. No addresses, no typed text; fire-and-forget.
export function track(event, extra = {}) {
  const body = JSON.stringify({ event, ...extra });
  try {
    if (navigator.sendBeacon?.("/api/events", new Blob([body], { type: "application/json" }))) return;
    fetch("/api/events", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true })
      .catch(() => {});
  } catch (_) { /* never let the log get in the way */ }
}

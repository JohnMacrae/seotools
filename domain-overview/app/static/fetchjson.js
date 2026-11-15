// The one way a page calls the API (CODING_STANDARDS.md "Every fetch fails visibly").
// getJSON resolves to the parsed body. On a network error or a non-2xx it throws an Error whose message is the
// API's `detail` when there is one, so the caller's catch can show it in an .err box. An empty list stays [];
// only a failure throws, so "no data" and "request failed" never look the same.
async function getJSON(url, opts) {
  let r;
  try { r = await fetch(url, opts); }
  catch (e) { throw new Error(`Network error (${e.message})`); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `${r.status} ${r.statusText}`.trim());
  return j;
}

// getJSON with a method and an optional JSON body.
function sendJSON(method, url, body) {
  return getJSON(url, body === undefined ? {method}
    : {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
}

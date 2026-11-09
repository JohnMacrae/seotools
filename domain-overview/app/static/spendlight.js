(function () {
  var css = ".spend-light{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:#8a8f98;margin:0 6px;vertical-align:middle}" +
    ".spend-light .sl-dot{width:9px;height:9px;border-radius:50%;display:inline-block;background:#9aa0a6}" +
    ".spend-light.green .sl-dot{background:#2e9d4f}.spend-light.red .sl-dot{background:#d64545}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  var cache = null;
  function parse(ts) {
    if (!ts) return null;
    var s = String(ts).trim().replace(" ", "T");
    if (!/(Z|[+-]\d\d:?\d\d)$/.test(s)) s += "Z";
    var d = new Date(s); return isNaN(d) ? null : d;
  }
  var M = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  function set(el, ts, cost, staleDays) {
    var d = parse(ts), sd = staleDays || (cache && cache.stale_days) || 30;
    var cls, txt, tip;
    if (!d) { cls = "never"; txt = "never refreshed"; tip = "Never refreshed"; }
    else {
      var age = (Date.now() - d.getTime()) / 86400000;
      cls = age <= sd ? "green" : "red";
      txt = "last refreshed " + d.getUTCDate() + " " + M[d.getUTCMonth()] + " " + d.getUTCFullYear();
      tip = "Last refresh " + d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
      if (cost != null && cost !== "" && !isNaN(+cost)) tip += " · cost $" + (+cost).toFixed(2);
    }
    el.className = "spend-light " + cls;
    el.title = tip;
    el.innerHTML = '<span class="sl-dot"></span><span class="sl-txt"></span>';
    el.lastChild.textContent = txt;
  }
  function render(el) {
    if (el.dataset.ts) return set(el, el.dataset.ts, el.dataset.cost);
    var f = cache && cache.features && cache.features[el.dataset.feature];
    if (!f || f.status === "never" || !f.last_at) return set(el, null);
    set(el, f.last_at, f.last_cost);
  }
  function refresh() {
    return fetch("/api/spend-status").then(function (r) { return r.json(); }).then(function (j) {
      cache = j;
      document.querySelectorAll(".spend-light[data-feature]").forEach(function (el) { render(el); });
    }).catch(function () {});
  }
  if (window.MutationObserver) new MutationObserver(function () {
    if (!cache) return;
    document.querySelectorAll(".spend-light[data-feature]").forEach(function (el) { if (!el.firstChild) render(el); });
  }).observe(document.documentElement, {childList: true, subtree: true});
  window.spendLights = { refresh: refresh, set: set };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", refresh); else refresh();
})();

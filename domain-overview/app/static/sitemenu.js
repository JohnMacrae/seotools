(function () {
  // Site menu: injected into every page header. Picking a site sets the cookie via ?site= and reloads.
  var css = ".site-menu{margin-left:auto;font-size:13px}.site-menu select{font:inherit;padding:3px 6px;border-radius:6px;" +
    "border:1px solid #9aa0a6;background:transparent;color:inherit}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  var ADD = "__add__";
  function build(j) {
    var host = document.querySelector("header .top") || document.querySelector("header") || document.body;
    var old = document.getElementById("site-menu"); if (old) old.remove();
    var box = document.createElement("label"); box.id = "site-menu"; box.className = "site-menu";
    box.appendChild(document.createTextNode("Site "));
    var sel = document.createElement("select");
    j.sites.forEach(function (s) {
      var o = document.createElement("option"); o.value = s.slug; o.textContent = s.name; sel.appendChild(o);
    });
    var add = document.createElement("option"); add.value = ADD; add.textContent = "+ Add site..."; sel.appendChild(add);
    sel.value = j.active;
    sel.onchange = function () {
      if (sel.value !== ADD) { location.search = "?site=" + encodeURIComponent(sel.value); return; }
      sel.value = j.active;
      var name = prompt("Site name (e.g. theweddingshop.uk)"); if (!name) return;
      var body = {
        name: name,
        gsc_property: prompt("Search Console property (e.g. sc-domain:example.com)") || "",
        repo_path: prompt("Repo path") || "",
        dfs_location: prompt("DataForSEO location code", "2826") || 2826
      };
      fetch("/api/sites", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
        .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
        .then(function (x) { if (!x[0]) alert(x[1].detail || "Could not add site"); else location.search = "?site=" + encodeURIComponent(x[1].slug); });
    };
    box.appendChild(sel); host.appendChild(box);
  }
  function init() {
    fetch("/api/sites").then(function (r) { return r.json(); }).then(build).catch(function () {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();

// Shared base-layout script, loaded on every page with `defer` from base.html.
// Lives in an external file (not an inline <script>) because the gateway sets a
// strict Content-Security-Policy ("default-src 'self'", no 'unsafe-inline') that
// blocks inline script execution; only same-origin /static files are allowed.
//
// Two responsibilities:
//   1. Parse the active-locale JSON catalog into window.I18N and expose
//      t(key, vars), mirroring the server interpolation contract. The <script
//      type="application/json" id="i18n-catalog"> block is data (not executed),
//      so it is unaffected by the CSP; only this logic had to move out of line.
//      Runs before the page's deferred account.js/upload.js (document order +
//      defer), so window.t is defined when they run.
//   2. Wire the nav Sign-out button: POST /auth/logout with the per-session
//      X-CSRF-Token header (the route is header-only, no form-field fallback).
(function () {
  var el = document.getElementById("i18n-catalog");
  window.I18N = el ? JSON.parse(el.textContent) : {};
  window.t = function (key, vars) {
    var value = window.I18N && window.I18N[key] != null ? window.I18N[key] : key;
    if (vars) {
      value = value.replace(/\{(\w+)\}/g, function (match, name) {
        return Object.prototype.hasOwnProperty.call(vars, name) ? vars[name] : match;
      });
    }
    return value;
  };

  var btn = document.querySelector("[data-logout]");
  if (!btn) return;
  btn.addEventListener("click", function () {
    btn.disabled = true;
    fetch("/auth/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": btn.getAttribute("data-csrf") || "" },
    })
      .then(function (response) {
        // Only treat a 2xx as signed-out; on failure (e.g. stale token)
        // re-enable the button rather than redirecting as if it worked.
        if (response.ok) {
          window.location.href = "/";
        } else {
          btn.disabled = false;
        }
      })
      .catch(function () {
        btn.disabled = false;
      });
  });
})();

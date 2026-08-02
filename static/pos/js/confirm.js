/* Offline-safe submit for money-critical forms (confirm / cancel a sale).
 *
 * Progressive enhancement over a plain <form method="post">: if JS fails to
 * load, the form still works as a normal full-page POST. With JS, a failed
 * fetch (offline, or the server unreachable) shows a clear Russian message
 * instead of the browser's generic "can't reach this page" screen — and,
 * critically, does NOT retry, queue, or otherwise pretend the sale went
 * through. If it didn't reach the server, it isn't saved.
 */
(function () {
  "use strict";

  var OFFLINE_MESSAGE = "Нет соединения. Продажа не сохранена — попробуйте ещё раз.";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-network-safe")) {
      return;
    }

    event.preventDefault();

    // The styled dialog in safety.js is async, so the fetch below moves into a
    // callback. Falls back to a bare window.confirm if safety.js failed to
    // load — a destructive action must never lose its guard just because one
    // script is missing.
    var confirmText = form.getAttribute("data-confirm");
    if (!confirmText) return send();
    var asked = window.posConfirm
      ? window.posConfirm(
          confirmText,
          form.getAttribute("data-confirm-ok"),
          null,
          form.getAttribute("data-confirm-tone")
        )
      : Promise.resolve(window.confirm(confirmText));
    asked.then(function (ok) {
      if (ok) send();
    });

    function send() {
    var button = form.querySelector("button[type=submit]");
    var errorTarget = form.getAttribute("data-error-target");
    var errorEl = errorTarget ? document.getElementById(errorTarget) : null;
    if (button) button.disabled = true;
    if (errorEl) errorEl.hidden = true;

    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) throw new Error("http " + response.status);
        window.location.href = response.url;
      })
      .catch(function () {
        if (errorEl) {
          errorEl.textContent = OFFLINE_MESSAGE;
          errorEl.hidden = false;
        } else {
          window.alert(OFFLINE_MESSAGE);
        }
        if (button) button.disabled = false;
      });
    }
  });
})();

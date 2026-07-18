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

    var confirmText = form.getAttribute("data-confirm");
    if (confirmText && !window.confirm(confirmText)) {
      event.preventDefault();
      return;
    }
    event.preventDefault();

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
  });
})();

/* Fixes a gap in django-jazzmin 3.0.5's own related-modal.js (/panel/ only —
 * loaded via JAZZMIN_SETTINGS["custom_js"], the hook the package ships for
 * exactly this).
 *
 * THE BUG: jazzmin's iframe popup for "+ add related object" (e.g. adding a
 * Category from the Product add form) never wires up the iframe's `.opener`
 * property — related-modal.js only does that for raw-id LOOKUP popups
 * (`{lookup: true}`), never for a plain add/change/delete popup
 * (`{lookup: false}`). Django's own admin, after a successful save inside a
 * popup, runs `opener.dismissAddRelatedObjectPopup(window, id, repr)` (see
 * django/contrib/admin/static/admin/js/popup_response.js) to update the
 * parent page's <select> and close the popup. With `.opener` never set,
 * that call throws (opener is undefined) — the new object WAS saved, but
 * the dropdown never gets it and the modal never closes. A manager seeing
 * nothing happen naturally tries again, creating a duplicate.
 *
 * THE FIX, in two parts:
 *   1. Wire `.opener` on EVERY related-modal iframe load, unconditionally —
 *      not just for lookups. This alone makes Django's own dismiss
 *      functions reachable again, so the select updates correctly.
 *   2. `window.close()` (which those functions call) is a no-op on an
 *      iframe's window — it only works on a real popup window — so the
 *      modal itself would still be left open. Wrap the three dismiss
 *      functions to also close jazzmin's modal afterward, completing the
 *      round trip the way a real popup window would.
 */
(function () {
  "use strict";

  document.addEventListener(
    "load",
    function (event) {
      var el = event.target;
      if (el && el.tagName === "IFRAME" && el.id === "related-modal-iframe") {
        try {
          el.contentWindow.opener = window;
        } catch (err) {
          /* same-origin admin popup — this never actually throws, but never
             let a wiring failure here break the rest of the page. */
        }
      }
    },
    true // capture phase: iframe "load" events don't bubble
  );

  ["dismissAddRelatedObjectPopup", "dismissChangeRelatedObjectPopup", "dismissDeleteRelatedObjectPopup"].forEach(
    function (name) {
      var original = window[name];
      if (typeof original !== "function") return;
      window[name] = function () {
        var result = original.apply(this, arguments);
        if (typeof window.dismissRelatedObjectModal === "function") {
          window.dismissRelatedObjectModal();
        }
        return result;
      };
    }
  );
})();

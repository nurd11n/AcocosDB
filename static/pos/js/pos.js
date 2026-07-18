/* Shared /pos/ behaviours, kept in one external file so the Content-Security-
 * Policy can stay strict (script-src 'self', no inline scripts or on* handlers).
 * Everything is wired by event delegation off data-attributes, so it no-ops
 * safely on pages that don't have the relevant elements (e.g. the label sheet).
 */
(function () {
  "use strict";

  // --- Theme toggle: name the *next* theme from what's actually rendered ---
  // (the server only knows the explicit cookie, not the OS-driven "auto" state).
  var themeForm = document.getElementById("theme-form");
  if (themeForm) {
    themeForm.addEventListener("submit", function () {
      var attr = document.documentElement.getAttribute("data-theme");
      var isDark = attr
        ? attr === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      var field = document.getElementById("theme-form-value");
      if (field) field.value = isDark ? "light" : "dark";
    });
  }

  // --- Delegated click handlers (replace former inline onclick=) ---
  document.addEventListener("click", function (event) {
    var el = event.target.closest("[data-print],[data-clear-target],[data-pay-amount]");
    if (!el) return;

    if (el.hasAttribute("data-print")) {
      window.print();
      return;
    }
    if (el.hasAttribute("data-clear-target")) {
      var target = document.getElementById(el.getAttribute("data-clear-target"));
      if (target) target.innerHTML = "";
      return;
    }
    if (el.hasAttribute("data-pay-amount")) {
      var input = document.getElementById("pay-amount");
      if (input) {
        input.value = el.getAttribute("data-pay-amount");
        if (window.htmx) window.htmx.trigger(input, "change");
      }
    }
  });

  // --- Desktop keyboard ---
  // "/" focuses the product search from anywhere; Esc closes the variant picker.
  // (Enter on a focused tile already opens its picker — native button activation
  // — and Tab order follows the visual order, so the whole sale is keyboard-only.)
  var search = document.getElementById("product-search");
  document.addEventListener("keydown", function (event) {
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (event.key === "/" && search && document.activeElement !== search && !typing) {
      event.preventDefault();
      search.focus();
    } else if (event.key === "Escape") {
      var picker = document.getElementById("variant-picker");
      if (picker && picker.innerHTML.trim()) picker.innerHTML = "";
    }
  });

  // --- Service worker (progressive; ignored where unsupported) ---
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/pos/sw.js", { scope: "/pos/" });
    });
  }
})();

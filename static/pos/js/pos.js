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
    // Click the variant-picker backdrop (the overlay itself, not the dialog
    // card inside it) to dismiss — standard modal behaviour.
    var picker = document.getElementById("variant-picker");
    if (picker && event.target === picker) {
      picker.innerHTML = "";
      return;
    }

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

  // --- Cmd/Ctrl+Z on the sale screen drives the cart's undo buttons -------
  // The basket is server state, so the keystroke just clicks the same button a
  // tap would (see apps/pos/undo.py) — one code path, and the button's own
  // disabled state is what decides whether anything happens. Ignored while the
  // caret is in a field, where the browser's native per-field undo is the
  // behaviour people actually expect.
  document.addEventListener("keydown", function (event) {
    var key = event.key ? event.key.toLowerCase() : "";
    if (key !== "z" || !(event.metaKey || event.ctrlKey)) return;
    var active = document.activeElement;
    if (active && active.closest && active.closest("input, textarea, select, [contenteditable]")) {
      return;
    }
    var scope = document.querySelector("[data-undo-scope]");
    if (!scope) return;
    var button = scope.querySelector(event.shiftKey ? "[data-redo-action]" : "[data-undo-action]");
    if (!button || button.disabled) return;
    event.preventDefault();
    button.click();
  });

  // --- Close a panel only AFTER its request finishes (data-clear-after) ---
  // data-clear-target above clears on the CLICK, which is right for a plain
  // «Отмена» but wrong for a button that fires a request: it tears the button
  // out of the DOM before htmx can put .htmx-request on it, so the busy label
  // never paints and the dialog just vanishes. The rate then lands a second
  // later with nothing in between, which reads as "the button did nothing".
  // Waiting for the response keeps the spinner visible for exactly as long as
  // the work takes, and makes the panel closing the confirmation that it
  // worked. A FAILED request deliberately leaves the panel open, so the error
  // is visible and the tap can be retried.
  document.addEventListener("htmx:afterRequest", function (event) {
    var el = event.target;
    if (!el || !el.getAttribute || !el.hasAttribute("data-clear-after")) return;
    if (!(event.detail && event.detail.successful)) return;
    var target = document.getElementById(el.getAttribute("data-clear-after"));
    if (target) target.innerHTML = "";
  });

  // --- Variant picker: quantity clamps to the CHOSEN variant's available
  // stock (Part 1a). Client-side only for instant feedback — the server
  // clamps and re-explains regardless, since this is UX, not the defence.
  // #variant-picker-input is a hidden field (not a <select>): the two-step
  // size -> color picker below is what sets its value and data-max. ---
  function clampPickerQty() {
    var variantInput = document.getElementById("variant-picker-input");
    var qtyInput = document.getElementById("picker-qty");
    var qtyNote = document.getElementById("picker-qty-note");
    if (!variantInput || !qtyInput) return;
    var max = parseInt(variantInput.getAttribute("data-max"), 10) || 0;
    qtyInput.max = String(max);
    var value = parseInt(qtyInput.value, 10) || 0;
    if (value > max) {
      qtyInput.value = String(max);
      if (qtyNote) {
        qtyNote.textContent = "Доступно только " + max + " шт.";
        qtyNote.hidden = false;
      }
    } else if (qtyNote) {
      qtyNote.hidden = true;
    }
  }
  document.addEventListener("change", function (event) {
    if (event.target.id === "picker-qty") clampPickerQty();
  });
  document.addEventListener("input", function (event) {
    if (event.target.id === "picker-qty") clampPickerQty();
  });

  // --- Variant picker: two-step size -> color selection, replacing a flat
  // <select> of every size/color/price/qty combination (~30 rows on a real
  // product — the actual complaint this replaces). Both steps' data arrive
  // in the ONE HTMX response that opens the picker (see
  // apps.pos.views._group_variants_for_picker); tapping a size just shows/
  // hides a <div data-colors-for> already sitting in the DOM — no further
  // request, since every variant for this product is already here. Picking
  // a variant — either straight from a single-color size chip, or from a
  // color tile — fills the hidden #variant-picker-input and reveals the
  // quantity step. ---
  function selectPickerVariant(variantId, max) {
    var variantInput = document.getElementById("variant-picker-input");
    var qtySection = document.getElementById("picker-qty-section");
    var qtyInput = document.getElementById("picker-qty");
    var submit = document.getElementById("picker-submit");
    if (!variantInput) return;
    variantInput.value = variantId;
    variantInput.setAttribute("data-max", String(max));
    if (qtyInput) qtyInput.value = "1";
    if (qtySection) qtySection.hidden = false;
    if (submit) submit.disabled = false;
    clampPickerQty();
  }
  function clearPickerSelection() {
    var variantInput = document.getElementById("variant-picker-input");
    var qtySection = document.getElementById("picker-qty-section");
    var submit = document.getElementById("picker-submit");
    if (variantInput) {
      variantInput.value = "";
      variantInput.setAttribute("data-max", "0");
    }
    if (qtySection) qtySection.hidden = true;
    if (submit) submit.disabled = true;
  }
  document.addEventListener("click", function (event) {
    var sizeChip = event.target.closest("[data-size-chip]");
    if (sizeChip) {
      if (sizeChip.disabled) return;
      var form = sizeChip.closest("form");
      if (!form) return;
      var chips = form.querySelectorAll("[data-size-chip]");
      for (var i = 0; i < chips.length; i++) chips[i].classList.remove("is-on");
      sizeChip.classList.add("is-on");

      var colorGroups = form.querySelectorAll("[data-colors-for]");
      for (var j = 0; j < colorGroups.length; j++) {
        var forSize = colorGroups[j].getAttribute("data-colors-for");
        colorGroups[j].hidden = forSize !== sizeChip.getAttribute("data-size");
      }

      if (sizeChip.hasAttribute("data-variant-id")) {
        selectPickerVariant(
          sizeChip.getAttribute("data-variant-id"),
          parseInt(sizeChip.getAttribute("data-max"), 10) || 0
        );
      } else {
        // Several colors under this size — nothing chosen yet. Clears any
        // selection a PREVIOUSLY tapped size may have already completed.
        clearPickerSelection();
      }
      return;
    }

    var colorTile = event.target.closest("[data-color-tile]");
    if (colorTile && !colorTile.disabled) {
      selectPickerVariant(
        colorTile.getAttribute("data-variant-id"),
        parseInt(colorTile.getAttribute("data-max"), 10) || 0
      );
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

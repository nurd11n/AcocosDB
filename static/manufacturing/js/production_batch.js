/* Batch production form — live "what am I about to save" preview.
 *
 * Nothing here computes a number that gets STORED: the authoritative split is
 * apps.manufacturing.services.split_cost_proportionally, server-side, in
 * Decimal. This is an estimate for the person typing, so a plain
 * toLocaleString is enough — no NBSP-grouping/cents-dropped parity with the
 * `money` filter is needed for a figure that never reaches the database.
 *
 * The maths mirrors the server's: a pure proportional-by-accepted-qty split
 * gives EVERY row the same per-unit cost (total ÷ total accepted), because
 * each row's own qty cancels out. So one number is correct for every row.
 */
(function () {
  "use strict";

  var FORM = "#production-batch-form";

  function parseAmount(raw) {
    var n = parseFloat(String(raw || "").replace(",", "."));
    return isFinite(n) && n > 0 ? n : 0;
  }

  function parseQty(raw) {
    var n = parseInt(raw, 10);
    return isFinite(n) && n > 0 ? n : 0;
  }

  function formatMoney(n) {
    return n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function currencySymbol(form) {
    var select = form.querySelector("#production-currency");
    var option = select && select.options[select.selectedIndex];
    return option ? option.getAttribute("data-symbol") || "" : "";
  }

  function recompute(form) {
    var totalCostInput = form.querySelector("#production-total-cost");
    var totalCost = parseAmount(totalCostInput && totalCostInput.value);

    var accepted = form.querySelectorAll('input[data-role="accepted"]');
    var qtyByVariant = {};
    var totalQty = 0;
    accepted.forEach(function (input) {
      var qty = parseQty(input.value);
      qtyByVariant[input.dataset.variant] = qty;
      totalQty += qty;
    });

    var perUnit = totalQty > 0 && totalCost > 0 ? totalCost / totalQty : 0;
    var symbol = currencySymbol(form);
    var perUnitText = perUnit > 0 ? "≈ " + formatMoney(perUnit) + " " + symbol + "/шт" : "";

    // Summary line: the units actually about to be recorded, and — only when a
    // cost was entered — what each accepted unit ends up costing.
    var previewEl = form.querySelector("#production-cost-preview");
    if (previewEl) {
      if (totalQty > 0) {
        var text = previewEl.dataset.labelUnits + ": " + totalQty + " шт";
        if (perUnit > 0) {
          text += " · " + previewEl.dataset.labelCost + " " + perUnitText;
        }
        previewEl.textContent = text;
      } else {
        previewEl.textContent = "";
      }
    }

    Object.keys(qtyByVariant).forEach(function (variantId) {
      var span = form.querySelector('[data-unit-cost="' + variantId + '"]');
      if (!span) return;
      span.textContent = qtyByVariant[variantId] > 0 ? perUnitText : "";
    });
  }

  function formFor(el) {
    return el && el.closest ? el.closest(FORM) : null;
  }

  document.addEventListener("input", function (e) {
    var form = formFor(e.target);
    if (!form) return;
    if (e.target.matches('input[data-role="accepted"]') || e.target.id === "production-total-cost") {
      recompute(form);
    }
  });

  document.addEventListener("change", function (e) {
    if (e.target.id !== "production-currency") return;
    var form = formFor(e.target);
    if (form) recompute(form);
  });

  // The grid arrives by HTMX swap (product picked), so wire it up on arrival —
  // and on first paint too, for the ?order_item=N / failed-submit renders that
  // ship the grid with the page.
  document.addEventListener("htmx:afterSwap", function (e) {
    var form = e.target.matches && e.target.matches(FORM) ? e.target : null;
    if (!form && e.target.querySelector) form = e.target.querySelector(FORM);
    if (form) recompute(form);
  });

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.querySelector(FORM);
    if (form) recompute(form);
  });
})();

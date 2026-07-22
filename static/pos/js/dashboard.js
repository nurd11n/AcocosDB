/* Dashboard interactivity — external so the strict CSP (script-src 'self') needs
 * no exception. Two jobs: count the metric numbers up, and drive the revenue
 * calendar (click a day cell -> show that day's revenue/sales/units in the
 * detail panel below). All respect prefers-reduced-motion. Re-runs after each
 * HTMX swap.
 */
(function () {
  "use strict";

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function groupNumber(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function countUp(root) {
    root.querySelectorAll("[data-count]").forEach(function (el) {
      var target = parseInt(el.getAttribute("data-count"), 10) || 0;
      // NBSP so the unit (" сом", " шт") never wraps away from its number.
      var suffix = (el.getAttribute("data-suffix") || "").replace(/ /g, " ");
      if (reduce) {
        el.textContent = groupNumber(target) + suffix;
        return;
      }
      var start = null;
      var dur = 500;
      function step(ts) {
        if (start === null) start = ts;
        var p = Math.min((ts - start) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 4); // ease-out-quart — snappier settle
        el.textContent = groupNumber(Math.round(target * eased)) + suffix;
        if (p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  var NBSP = " ";

  function setupCalendar(root) {
    var cal = root.querySelector("[data-cal]");
    var detail = root.querySelector("[data-cal-detail]");
    if (!cal || !detail) return;
    var symbol = cal.getAttribute("data-cur") || "сом";
    var hint = detail.querySelector("[data-d-hint]");
    var dDate = detail.querySelector("[data-d-date]");
    var dStats = detail.querySelector("[data-d-stats]");
    var dRev = detail.querySelector("[data-d-rev]");
    var dUnits = detail.querySelector("[data-d-units]");
    var dOrders = detail.querySelector("[data-d-orders]");
    var activeCell = null;

    function show(btn) {
      if (activeCell) activeCell.classList.remove("is-active");
      activeCell = btn;
      btn.classList.add("is-active");
      dDate.textContent = btn.getAttribute("data-date");
      dRev.textContent = groupNumber(btn.getAttribute("data-rev")) + NBSP + symbol;
      dUnits.textContent = groupNumber(btn.getAttribute("data-units")) + NBSP + "шт";
      dOrders.textContent = btn.getAttribute("data-orders");
      if (hint) hint.hidden = true;
      dDate.hidden = false;
      dStats.hidden = false;
    }

    var cells = cal.querySelectorAll("[data-day]");
    cells.forEach(function (btn) {
      btn.addEventListener("click", function () { show(btn); });
    });

    // Auto-open the most recent day that had sales (last with rev > 0 in DOM
    // order = latest date), so the panel is never empty when there's data.
    var withSales = null;
    for (var i = cells.length - 1; i >= 0; i--) {
      if (parseInt(cells[i].getAttribute("data-rev"), 10) > 0) { withSales = cells[i]; break; }
    }
    if (withSales) show(withSales);
  }

  // The period + currency controls now live inside the swapped panel and are
  // server-rendered with the correct aria-pressed each time, so no client-side
  // pill syncing is needed.

  function init(root) {
    countUp(root);
    setupCalendar(root);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init(document);
  });
  // Re-init the swapped panel after an HTMX period/currency change.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    init(e.target);
  });
})();

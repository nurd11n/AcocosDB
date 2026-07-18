/* Dashboard interactivity — external so the strict CSP (script-src 'self') needs
 * no exception. Three jobs: set the chart line's dash length so the draw-in
 * animation works, count metric numbers up, and show a tooltip on the revenue
 * chart. All respect prefers-reduced-motion. Re-runs after each HTMX swap.
 */
(function () {
  "use strict";

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function groupNumber(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function setupChartDraw(root) {
    // stroke-dashoffset needs the path length; feed it to the CSS var --len.
    root.querySelectorAll("[data-line]").forEach(function (path) {
      try {
        var len = Math.ceil(path.getTotalLength());
        path.style.setProperty("--len", len);
      } catch (e) {
        /* getTotalLength can throw on a detached node — ignore */
      }
    });
  }

  function countUp(root) {
    root.querySelectorAll("[data-count]").forEach(function (el) {
      var target = parseInt(el.getAttribute("data-count"), 10) || 0;
      var suffix = el.getAttribute("data-suffix") || "";
      if (reduce) {
        el.textContent = groupNumber(target) + suffix;
        return;
      }
      var start = null;
      var dur = 500;
      function step(ts) {
        if (start === null) start = ts;
        var p = Math.min((ts - start) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 3); // ease-out
        el.textContent = groupNumber(Math.round(target * eased)) + suffix;
        if (p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  function setupTooltip(root) {
    var wrap = root.querySelector(".chart-wrap");
    if (!wrap) return;
    var svg = wrap.querySelector("svg");
    var tip = wrap.querySelector("[data-tooltip]");
    var guide = wrap.querySelector("[data-guide]");
    if (!svg || !tip) return;

    function show(rect) {
      var label = rect.getAttribute("data-label");
      var value = rect.getAttribute("data-value");
      tip.innerHTML = "<strong>" + value + "</strong><br>" + label;
      tip.style.visibility = "visible";
      // Position the tooltip near the point, in the wrapper's pixel space.
      var box = svg.getBoundingClientRect();
      var vb = svg.viewBox.baseVal;
      var cx = parseFloat(rect.getAttribute("data-cx"));
      var px = (cx / vb.width) * box.width;
      tip.style.left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth) + "px";
      tip.style.top = "0px";
      if (guide) {
        guide.setAttribute("x1", cx);
        guide.setAttribute("x2", cx);
        guide.style.visibility = "visible";
      }
    }
    function hide() {
      tip.style.visibility = "hidden";
      if (guide) guide.style.visibility = "hidden";
    }
    root.querySelectorAll("[data-pt]").forEach(function (rect) {
      rect.addEventListener("mouseenter", function () { show(rect); });
      rect.addEventListener("focus", function () { show(rect); });
    });
    svg.addEventListener("mouseleave", hide);
  }

  function init(root) {
    setupChartDraw(root);
    countUp(root);
    setupTooltip(root);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init(document);
  });
  // Re-init the swapped panel after an HTMX period change.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    init(e.target);
  });
})();

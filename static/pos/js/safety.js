/* Safety net for accidental mistakes — three independent pieces, all opt-in
 * from markup so nothing changes on a page that doesn't ask for it:
 *
 *   [data-confirm="…"]        a styled confirmation before a destructive action
 *   <form data-autosave="k">  field values survive an accidental navigation
 *   <form data-undo>          Cmd/Ctrl+Z and Cmd/Ctrl+Shift+Z across the form
 *
 * Kept in one external file so the Content-Security-Policy stays strict
 * (script-src 'self', no inline scripts, no on* handlers). Everything is wired
 * by event delegation off data-attributes, so it no-ops safely on pages that
 * don't have the relevant elements.
 *
 * NOTE ON SCOPE: this guards the EDITING stage — a draft cart, a form being
 * filled in. Anything already committed (a confirmed sale, a recorded payment,
 * a stock movement) is deliberately NOT undoable from here: those reverse
 * through services.cancel_sale / return_items / void_payment, which write a
 * reversing ledger entry and keep the audit trail. A keystroke must never
 * silently rewrite money that's already been counted.
 */
(function () {
  "use strict";

  // --- Styled confirmation ------------------------------------------------
  // window.confirm() is synchronous and unstyleable; a native <dialog> is
  // focus-trapped and ESC-closable for free. Falls back to window.confirm
  // where showModal() is missing, so the guard can never silently vanish and
  // let a destructive action through unchallenged.
  var dialog = null;

  function buildDialog() {
    var el = document.createElement("dialog");
    el.className = "confirm-dialog";
    el.innerHTML =
      '<p class="confirm-dialog__text"></p>' +
      '<div class="confirm-dialog__actions">' +
      '<button type="button" class="btn btn-ghost" data-confirm-no></button>' +
      '<button type="button" class="btn btn-ghost btn-ghost--debt" data-confirm-yes></button>' +
      "</div>";
    document.body.appendChild(el);
    return el;
  }

  function ask(message, okLabel, cancelLabel) {
    if (!window.HTMLDialogElement || !document.body) {
      return Promise.resolve(window.confirm(message));
    }
    if (!dialog) dialog = buildDialog();
    dialog.querySelector(".confirm-dialog__text").textContent = message;
    var yes = dialog.querySelector("[data-confirm-yes]");
    var no = dialog.querySelector("[data-confirm-no]");
    yes.textContent = okLabel || "Да, удалить";
    no.textContent = cancelLabel || "Отмена";

    return new Promise(function (resolve) {
      var settled = false;
      function finish(answer) {
        if (settled) return;
        settled = true;
        yes.removeEventListener("click", onYes);
        no.removeEventListener("click", onNo);
        dialog.removeEventListener("close", onClose);
        if (dialog.open) dialog.close();
        resolve(answer);
      }
      function onYes() {
        finish(true);
      }
      function onNo() {
        finish(false);
      }
      // ESC / backdrop dismiss must read as "no" — never as a silent yes.
      function onClose() {
        finish(false);
      }
      yes.addEventListener("click", onYes);
      no.addEventListener("click", onNo);
      dialog.addEventListener("close", onClose);
      dialog.showModal();
      no.focus(); // the safe option is the one under the finger by default
    });
  }

  window.posConfirm = ask;

  // htmx path: htmx fires htmx:confirm before EVERY request, so this is the
  // one hook that covers every hx-post/hx-get on the page. We read
  // data-confirm rather than htmx's own hx-confirm, because hx-confirm would
  // pop an unstyled window.confirm before we ever get here.
  document.addEventListener("htmx:confirm", function (event) {
    var elt = event.detail && event.detail.elt;
    if (!elt || !elt.getAttribute) return;
    var message = elt.getAttribute("data-confirm");
    if (!message) return;
    event.preventDefault();
    ask(message, elt.getAttribute("data-confirm-ok")).then(function (ok) {
      if (ok) event.detail.issueRequest(true);
    });
  });

  // Plain (non-htmx) form path. data-network-safe forms are handled by
  // confirm.js, which calls window.posConfirm itself — skipped here so the
  // question is never asked twice.
  document.addEventListener(
    "submit",
    function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.hasAttribute("data-network-safe")) return;
      var message = form.getAttribute("data-confirm");
      if (!message || form.dataset.confirmed === "1") return;
      event.preventDefault();
      ask(message, form.getAttribute("data-confirm-ok")).then(function (ok) {
        if (!ok) return;
        form.dataset.confirmed = "1";
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else form.submit();
      });
    },
    true // capture: run before any other submit handler can act on it
  );

  // --- Form field helpers -------------------------------------------------
  function fields(form) {
    return Array.prototype.filter.call(
      form.querySelectorAll("input, select, textarea"),
      function (el) {
        return (
          el.name &&
          el.type !== "hidden" &&
          el.type !== "password" &&
          el.type !== "file" &&
          el.type !== "submit"
        );
      }
    );
  }

  function readForm(form) {
    var state = {};
    fields(form).forEach(function (el) {
      state[el.name] = el.type === "checkbox" || el.type === "radio" ? el.checked : el.value;
    });
    return state;
  }

  function writeForm(form, state) {
    fields(form).forEach(function (el) {
      if (!(el.name in state)) return;
      if (el.type === "checkbox" || el.type === "radio") el.checked = !!state[el.name];
      else el.value = state[el.name];
    });
  }

  // --- Autosave: survive an accidental navigation -------------------------
  // Only ever restores into a form the user has NOT already started filling
  // from the server (a populated edit form wins), and clears the moment the
  // form is submitted, so a saved record never gets shadowed by a stale draft.
  function autosaveKey(form) {
    return "acocos:draft:" + form.getAttribute("data-autosave") + ":" + window.location.pathname;
  }

  function initAutosave(form) {
    var key = autosaveKey(form);
    try {
      var saved = window.localStorage.getItem(key);
      if (saved) {
        var state = JSON.parse(saved);
        var current = readForm(form);
        var differs = Object.keys(state).some(function (name) {
          return name in current && String(current[name]) !== String(state[name]);
        });
        if (differs) {
          writeForm(form, state);
          form.dispatchEvent(new CustomEvent("acocos:restored", { bubbles: true }));
        }
      }
    } catch (e) {
      /* private mode / quota / corrupt JSON — autosave is a bonus, never a blocker */
    }

    form.addEventListener("input", function () {
      try {
        window.localStorage.setItem(key, JSON.stringify(readForm(form)));
      } catch (e) {
        /* ignore */
      }
    });
    form.addEventListener("submit", function () {
      try {
        window.localStorage.removeItem(key);
      } catch (e) {
        /* ignore */
      }
    });
  }

  // --- Form-level undo / redo --------------------------------------------
  // The browser's native Cmd+Z only reaches inside the ONE text field that has
  // focus — it cannot undo a <select> change, a checkbox, or an edit you made
  // in a field you've since tabbed away from. This keeps a stack of whole-form
  // snapshots so Cmd+Z walks back through every change in order, wherever it
  // happened.
  var UNDO_LIMIT = 50;

  function initUndo(form) {
    var undo = [readForm(form)];
    var redo = [];
    var timer = null;

    function snapshot() {
      var state = readForm(form);
      var last = undo[undo.length - 1];
      if (JSON.stringify(last) === JSON.stringify(state)) return;
      undo.push(state);
      if (undo.length > UNDO_LIMIT) undo.shift();
      redo = [];
    }

    // Typing coalesces: one snapshot per pause, so Cmd+Z steps back a word or
    // a field, never one character at a time.
    form.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(snapshot, 400);
    });
    form.addEventListener("change", function () {
      window.clearTimeout(timer);
      snapshot();
    });
    form.addEventListener("acocos:restored", snapshot);

    form.addEventListener("keydown", function (event) {
      var key = event.key ? event.key.toLowerCase() : "";
      if (key !== "z" || !(event.metaKey || event.ctrlKey)) return;
      window.clearTimeout(timer);
      if (event.shiftKey) {
        if (!redo.length) return;
        event.preventDefault();
        undo.push(redo.pop());
        writeForm(form, undo[undo.length - 1]);
      } else {
        snapshot();
        if (undo.length < 2) return;
        event.preventDefault();
        redo.push(undo.pop());
        writeForm(form, undo[undo.length - 1]);
      }
    });
  }

  function init(root) {
    var scope = root && root.querySelectorAll ? root : document;
    Array.prototype.forEach.call(scope.querySelectorAll("form[data-autosave]"), function (form) {
      if (form.dataset.autosaveReady) return;
      form.dataset.autosaveReady = "1";
      initAutosave(form);
    });
    Array.prototype.forEach.call(scope.querySelectorAll("form[data-undo]"), function (form) {
      if (form.dataset.undoReady) return;
      form.dataset.undoReady = "1";
      initUndo(form);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      init(document);
    });
  } else {
    init(document);
  }
  // Forms swapped in by htmx need wiring too.
  document.addEventListener("htmx:afterSwap", function (event) {
    init(event.target);
  });
})();

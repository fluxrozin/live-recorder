(function () {
  "use strict";

  var doc = document;
  var iconSprite = (doc.querySelector('meta[name="lr-icons"]') || {}).content || "/static/icons.svg";

  function $(selector, context) {
    return (context || doc).querySelector(selector);
  }

  function $all(selector, context) {
    return Array.prototype.slice.call((context || doc).querySelectorAll(selector));
  }

  function icon(name, extra) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = doc.createElementNS(ns, "svg");
    svg.setAttribute("class", "c-icon" + (extra ? " " + extra : ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var use = doc.createElementNS(ns, "use");
    use.setAttribute("href", iconSprite + "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function setIcon(svg, name) {
    var use = svg && svg.querySelector("use");
    if (use) use.setAttribute("href", iconSprite + "#i-" + name);
  }

  function pad(value) {
    return value < 10 ? "0" + value : String(value);
  }

  function nowClock() {
    var d = new Date();
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  /* ===== Toast ===== */
  var TOAST_ICONS = { success: "circle-check", info: "info", warning: "triangle-alert", danger: "circle-x" };

  function dismissToast(el) {
    if (!el || el.classList.contains("is-leaving")) return;
    el.classList.add("is-leaving");
    window.setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 220);
  }

  function armToast(el) {
    var tone = el.getAttribute("data-tone") || "success";
    var delay = tone === "danger" ? 9000 : tone === "warning" ? 7000 : 4500;
    var timer = null;
    function start() {
      timer = window.setTimeout(function () {
        dismissToast(el);
      }, delay);
    }
    function stop() {
      if (timer) window.clearTimeout(timer);
    }
    el.addEventListener("mouseenter", stop);
    el.addEventListener("focusin", stop);
    el.addEventListener("mouseleave", start);
    el.addEventListener("focusout", start);
    var close = el.querySelector("[data-toast-close]");
    if (close) {
      close.addEventListener("click", function () {
        dismissToast(el);
      });
    }
    start();
  }

  function toast(message, tone) {
    tone = tone || "success";
    var region = $("#toast-region");
    if (!region) return;
    var el = doc.createElement("div");
    el.className = "c-toast c-toast--" + tone;
    el.setAttribute("data-tone", tone);
    if (tone === "danger") el.setAttribute("role", "alert");
    el.appendChild(icon(TOAST_ICONS[tone] || "info"));
    var text = doc.createElement("div");
    text.className = "c-toast__text";
    text.textContent = message;
    el.appendChild(text);
    var close = doc.createElement("button");
    close.type = "button";
    close.className = "c-icon-btn c-icon-btn--sm";
    close.setAttribute("aria-label", "通知を閉じる");
    close.setAttribute("data-toast-close", "");
    close.appendChild(icon("x", "c-icon--sm"));
    el.appendChild(close);
    region.appendChild(el);
    armToast(el);
  }

  window.LR = { toast: toast };

  /* ===== Drawer (mobile sidebar) ===== */
  var drawerQuery = window.matchMedia("(max-width: 1023px)");
  var sidebar = $("#sidebar");
  var backdrop = $("#sidebar-backdrop");
  var mainRegion = $("#app-main");
  var drawerOpener = null;

  function setDrawer(open) {
    if (!sidebar) return;
    var opener = $("[data-drawer-open]");
    sidebar.classList.toggle("is-open", open);
    if (backdrop) backdrop.hidden = !open;
    if (opener) opener.setAttribute("aria-expanded", open ? "true" : "false");
    if (mainRegion) {
      if (open) mainRegion.setAttribute("inert", "");
      else mainRegion.removeAttribute("inert");
    }
    if (open) {
      drawerOpener = doc.activeElement;
      var first = $('.c-nav__item[aria-current="page"]', sidebar) || $(".c-nav__item", sidebar);
      if (first) first.focus();
    } else if (drawerOpener && drawerOpener.focus) {
      drawerOpener.focus();
      drawerOpener = null;
    }
  }

  doc.addEventListener("click", function (event) {
    if (event.target.closest("[data-drawer-open]")) {
      setDrawer(true);
    } else if (event.target.closest("[data-drawer-close]")) {
      setDrawer(false);
    }
  });

  if (sidebar) {
    sidebar.addEventListener("keydown", function (event) {
      if (!sidebar.classList.contains("is-open")) return;
      if (event.key === "Escape") {
        setDrawer(false);
        return;
      }
      if (event.key !== "Tab") return;
      var focusables = $all("a[href], button:not([disabled]), input:not([disabled])", sidebar).filter(function (el) {
        return el.offsetParent !== null || el.getClientRects().length;
      });
      if (!focusables.length) return;
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      if (event.shiftKey && doc.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && doc.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  function onDrawerQuery() {
    if (!drawerQuery.matches && sidebar && sidebar.classList.contains("is-open")) setDrawer(false);
  }
  if (drawerQuery.addEventListener) drawerQuery.addEventListener("change", onDrawerQuery);

  /* ===== Theme switch ===== */
  function initTheme() {
    if (!window.LRTheme) return;
    var current = window.LRTheme.get();
    $all('[data-theme-switch] input[name="theme"]').forEach(function (input) {
      input.checked = input.value === current;
      input.addEventListener("change", function () {
        if (input.checked) window.LRTheme.set(input.value);
      });
    });
  }

  /* ===== Dropdown menu ===== */
  var openMenuState = null;

  function menuItems(list) {
    return $all('[role="menuitem"]:not([aria-disabled="true"])', list);
  }

  function positionMenu(trigger, list) {
    var rect = trigger.getBoundingClientRect();
    var width = list.offsetWidth;
    var height = list.offsetHeight;
    var left = rect.right - width;
    if (list.getAttribute("data-align") === "start") left = rect.left;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    var top = rect.bottom + 4;
    if (top + height > window.innerHeight - 8 && rect.top - height - 4 > 8) top = rect.top - height - 4;
    list.style.left = Math.round(left) + "px";
    list.style.top = Math.round(top) + "px";
  }

  function closeMenu(returnFocus) {
    if (!openMenuState) return;
    var state = openMenuState;
    openMenuState = null;
    state.list.hidden = true;
    state.trigger.setAttribute("aria-expanded", "false");
    if (returnFocus) state.trigger.focus();
  }

  function openMenu(trigger, focusFirst) {
    var list = doc.getElementById(trigger.getAttribute("aria-controls"));
    if (!list) return;
    closeMenu(false);
    list.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    positionMenu(trigger, list);
    openMenuState = { trigger: trigger, list: list };
    if (focusFirst) {
      var items = menuItems(list);
      if (items.length) items[0].focus();
    }
  }

  doc.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-menu-trigger]");
    if (trigger) {
      event.preventDefault();
      if (openMenuState && openMenuState.trigger === trigger) closeMenu(false);
      else openMenu(trigger, event.detail === 0);
      return;
    }
    if (openMenuState) {
      var inside = event.target.closest(".c-menu__list");
      if (!inside || event.target.closest('[role="menuitem"]')) closeMenu(false);
    }
  });

  doc.addEventListener("keydown", function (event) {
    var trigger = event.target.closest && event.target.closest("[data-menu-trigger]");
    if (trigger && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      openMenu(trigger, true);
      if (event.key === "ArrowUp" && openMenuState) {
        var all = menuItems(openMenuState.list);
        if (all.length) all[all.length - 1].focus();
      }
      return;
    }
    if (!openMenuState) return;
    var items = menuItems(openMenuState.list);
    var index = items.indexOf(doc.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
    } else if (event.key === "Tab") {
      closeMenu(false);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
    } else if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
    } else if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
    }
  });

  window.addEventListener("resize", function () {
    closeMenu(false);
  });
  window.addEventListener(
    "scroll",
    function () {
      if (openMenuState) positionMenu(openMenuState.trigger, openMenuState.list);
    },
    true
  );

  /* ===== Dialog ===== */
  var dialogReturn = null;

  function openDialog(dialog, trigger) {
    if (!dialog || typeof dialog.showModal !== "function") return;
    if (trigger) {
      var action = trigger.getAttribute("data-dialog-action");
      var form = dialog.querySelector("form");
      if (action && form) form.setAttribute("action", action);
      Array.prototype.forEach.call(trigger.attributes, function (attr) {
        if (attr.name.indexOf("data-fill-") !== 0) return;
        var key = attr.name.slice("data-fill-".length);
        $all('[data-fill="' + key + '"]', dialog).forEach(function (target) {
          target.textContent = attr.value;
        });
      });
    }
    dialogReturn = trigger || doc.activeElement;
    dialog.showModal();
    var focusTarget = dialog.querySelector("[autofocus], [aria-invalid='true']");
    if (focusTarget) focusTarget.focus();
  }

  doc.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-dialog-open]");
    if (opener) {
      event.preventDefault();
      openDialog(doc.getElementById(opener.getAttribute("data-dialog-open")), opener);
      return;
    }
    var closer = event.target.closest("[data-dialog-close]");
    if (closer) {
      var dialog = closer.closest("dialog");
      if (dialog) dialog.close();
      return;
    }
    if (event.target.tagName === "DIALOG" && event.target.open) {
      var rect = event.target.getBoundingClientRect();
      var outside =
        event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
      if (outside) event.target.close();
    }
  });

  function initDialogs(root) {
    $all("dialog.c-dialog", root).forEach(function (dialog) {
      if (dialog.hasAttribute("data-ready")) return;
      dialog.setAttribute("data-ready", "");
      dialog.addEventListener("close", function () {
        $all("[data-reset-on-close]", dialog).forEach(function (input) {
          if (input.type === "checkbox") input.checked = false;
        });
        $all("button[type='submit']", dialog).forEach(syncAckButton);
        if (dialogReturn && doc.contains(dialogReturn) && dialogReturn.focus) dialogReturn.focus();
        dialogReturn = null;
      });
      if (dialog.hasAttribute("data-open-on-load")) openDialog(dialog, null);
    });
  }

  /* 確認チェックボックスが必要な削除ボタンは、チェックするまで押せないようにする */
  function syncAckButton(button) {
    var form = button.form;
    if (!form) return;
    var acks = $all("[data-ack]", form);
    if (!acks.length) return;
    button.disabled = !acks.every(function (input) {
      return input.checked;
    });
  }

  doc.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches("[data-ack]") && event.target.form) {
      $all("button[type='submit']", event.target.form).forEach(syncAckButton);
    }
  });

  /* 送信中は二重送信を防ぎ、処理中であることを表示する */
  doc.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute("data-busy")) return;
    var button = event.submitter || form.querySelector("button[type='submit']");
    window.setTimeout(function () {
      if (!button) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      var spinner = doc.createElement("span");
      spinner.className = "c-spinner";
      spinner.setAttribute("aria-hidden", "true");
      button.insertBefore(spinner, button.firstChild);
    }, 0);
  });

  /* ===== HTMX integration ===== */
  var REQUEST_ERRORS = {
    401: "ログインの有効期限が切れました。再度ログインしてください",
    403: "セキュリティトークンが無効です。ページを再読み込みしてから操作してください",
    404: "対象が見つかりません。すでに削除された可能性があります",
  };

  function requestErrorMessage(xhr) {
    if (!xhr || !xhr.status) return "サーバーに接続できません。ネットワークとコンテナの状態を確認してください";
    return REQUEST_ERRORS[xhr.status] || "サーバーでエラーが発生しました（HTTP " + xhr.status + "）";
  }

  function isLoginRedirect(xhr) {
    if (!xhr || !xhr.responseURL) return false;
    try {
      return new URL(xhr.responseURL).pathname === "/login";
    } catch (err) {
      return false;
    }
  }

  doc.addEventListener("htmx:configRequest", function (event) {
    var token = doc.querySelector('meta[name="csrf-token"]');
    if (token) event.detail.headers["X-CSRF-Token"] = token.content;
  });

  doc.addEventListener("htmx:beforeRequest", function (event) {
    var elt = event.detail.elt;
    if (elt && elt.hasAttribute && elt.hasAttribute("data-poll") && doc.visibilityState === "hidden") {
      event.preventDefault();
    }
    closeMenu(false);
  });

  var pendingToggle = null;

  doc.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    if (isLoginRedirect(xhr)) {
      event.detail.shouldSwap = false;
      window.location.assign("/login");
      return;
    }
    var source = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (source && source.hasAttribute && source.hasAttribute("data-toggle") && xhr.status === 200) {
      pendingToggle = {
        rowId: source.getAttribute("data-row"),
        field: source.getAttribute("data-field"),
        label: source.getAttribute("data-label"),
        name: source.getAttribute("data-name"),
      };
    }
  });

  doc.addEventListener("htmx:afterSettle", function (event) {
    if (pendingToggle) {
      var info = pendingToggle;
      pendingToggle = null;
      var row = doc.getElementById(info.rowId);
      if (row) {
        var sw = row.querySelector('[data-field="' + info.field + '"] [role="switch"]');
        var on = sw && sw.getAttribute("aria-checked") === "true";
        row.classList.add("is-flash");
        toast(info.name + "：" + info.label + "を" + (on ? "ON" : "OFF") + "にしました", "success");
        if (sw) sw.focus();
      }
    }
    var target = event.detail.target;
    if (target && target.id === "results") {
      var results = document.getElementById("results");
      if (results) {
        var heading = $("[data-results-heading]", results);
        if (results.getBoundingClientRect().top < 0) results.scrollIntoView({ block: "start" });
        if (heading) heading.focus({ preventScroll: true });
      }
    }
  });

  function handleRequestFailure(event) {
    var elt = event.detail.elt;
    var xhr = event.detail.xhr;
    if (isLoginRedirect(xhr)) {
      window.location.assign("/login");
      return;
    }
    var message = requestErrorMessage(xhr);
    if (elt && elt.hasAttribute && elt.hasAttribute("data-poll")) {
      var status = $("[data-poll-status]", elt);
      if (status) {
        status.textContent = "更新に失敗しました（" + nowClock() + "）。表示中の情報は古い可能性があります";
        status.classList.add("is-stale");
      }
      return;
    }
    if (elt && elt.hasAttribute && elt.hasAttribute("data-toggle")) {
      var label = elt.getAttribute("data-label") || "設定";
      message = (elt.getAttribute("data-name") || "") + "：" + label + "を切り替えられませんでした。" + message;
      var row = elt.closest("tr");
      var slot = row && row.querySelector("[data-row-error]");
      if (slot) {
        slot.hidden = false;
        var text = slot.querySelector("[data-row-error-text]");
        if (text) text.textContent = label + "の切り替えに失敗しました（状態は変更されていません）";
      }
    }
    toast(message, "danger");
  }

  doc.addEventListener("htmx:responseError", handleRequestFailure);
  doc.addEventListener("htmx:sendError", handleRequestFailure);

  doc.addEventListener("htmx:load", function (event) {
    initDialogs(event.detail.elt);
  });

  doc.addEventListener("visibilitychange", function () {
    if (doc.visibilityState !== "visible" || !window.htmx) return;
    $all("[data-poll]").forEach(function (el) {
      var last = Number(el.getAttribute("data-poll-at") || 0);
      var interval = Number(el.getAttribute("data-poll") || 30) * 1000;
      if (Date.now() - last > interval) window.htmx.trigger(el, "lr:refresh");
    });
  });

  /* ===== Elapsed timers ===== */
  function formatElapsed(ms) {
    var total = Math.max(0, Math.floor(ms / 1000));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    return (h ? h + ":" + pad(m) : String(m)) + ":" + pad(s);
  }

  function tickElapsed() {
    var now = Date.now();
    $all("[data-elapsed-since]").forEach(function (el) {
      var since = Date.parse(el.getAttribute("data-elapsed-since"));
      if (!isNaN(since)) el.textContent = formatElapsed(now - since);
    });
  }
  window.setInterval(tickElapsed, 1000);

  /* ===== Tabs ===== */
  function initTabs(root) {
    $all("[data-tabs]", root).forEach(function (container) {
      if (container.hasAttribute("data-ready")) return;
      container.setAttribute("data-ready", "");
      var list = $('[role="tablist"]', container);
      if (!list) return;
      var tabs = $all('[role="tab"]', list);
      var useHash = container.hasAttribute("data-tabs-hash");
      list.hidden = false;
      container.classList.add("is-tabbed");

      function select(tab, focus, keepHash) {
        tabs.forEach(function (other) {
          var selected = other === tab;
          other.setAttribute("aria-selected", selected ? "true" : "false");
          other.tabIndex = selected ? 0 : -1;
          var panel = doc.getElementById(other.getAttribute("aria-controls"));
          if (panel) panel.hidden = !selected;
        });
        if (focus) tab.focus();
        var panelId = tab.getAttribute("aria-controls") || "";
        var remembered = $("input[name='active_tab']", container);
        if (remembered) remembered.value = panelId;
        if (useHash && !keepHash && window.history.replaceState) {
          window.history.replaceState(null, "", "#" + panelId);
        }
      }

      var initial = tabs[0];
      if (useHash && window.location.hash) {
        var fromHash = tabs.filter(function (tab) {
          return "#" + tab.getAttribute("aria-controls") === window.location.hash;
        })[0];
        if (fromHash) initial = fromHash;
      }
      var preselected = tabs.filter(function (tab) {
        return tab.getAttribute("aria-selected") === "true";
      })[0];
      if (!useHash || !window.location.hash) initial = preselected || initial;
      select(initial, false, true);

      list.addEventListener("click", function (event) {
        var tab = event.target.closest('[role="tab"]');
        if (tab) select(tab, false);
      });
      list.addEventListener("keydown", function (event) {
        var index = tabs.indexOf(doc.activeElement);
        if (index < 0) return;
        var next = null;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") next = tabs[(index + 1) % tabs.length];
        if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = tabs[(index - 1 + tabs.length) % tabs.length];
        if (event.key === "Home") next = tabs[0];
        if (event.key === "End") next = tabs[tabs.length - 1];
        if (next) {
          event.preventDefault();
          select(next, true);
        }
      });
    });
  }

  /* ===== Settings: unsaved changes ===== */
  function fieldValue(el) {
    if (el.type === "checkbox" || el.type === "radio") return el.checked ? "1" : "0";
    return el.value;
  }

  function initDirtyForms() {
    $all("form[data-dirty-form]").forEach(function (form) {
      var fields = $all("input:not([type='hidden']), select, textarea", form);
      var initial = new Map();
      fields.forEach(function (el) {
        initial.set(el, fieldValue(el));
      });
      var bar = $("[data-savebar]", form);
      var statusText = $("[data-savebar-text]", form);
      var submitting = false;

      function refresh() {
        var dirtyPanels = {};
        var count = 0;
        fields.forEach(function (el) {
          if (fieldValue(el) !== initial.get(el)) {
            count += 1;
            var panel = el.closest("[role='tabpanel']");
            if (panel) dirtyPanels[panel.id] = true;
          }
        });
        if (bar) bar.classList.toggle("is-dirty", count > 0);
        if (statusText) statusText.textContent = count ? "未保存の変更が " + count + " 件あります" : "変更はありません";
        $all("[data-dirty-dot]", form.ownerDocument).forEach(function (dot) {
          dot.hidden = !dirtyPanels[dot.getAttribute("data-dirty-dot")];
        });
        form.setAttribute("data-dirty", count ? "true" : "false");
      }

      form.addEventListener("input", refresh);
      form.addEventListener("change", refresh);
      form.addEventListener("reset", function () {
        window.setTimeout(refresh, 0);
      });
      form.addEventListener("submit", function () {
        submitting = true;
      });
      window.addEventListener("beforeunload", function (event) {
        if (!submitting && form.getAttribute("data-dirty") === "true") {
          event.preventDefault();
          event.returnValue = "";
        }
      });
      refresh();
    });
  }

  /* ===== Password visibility ===== */
  doc.addEventListener("click", function (event) {
    var button = event.target.closest("[data-password-toggle]");
    if (!button) return;
    var input = doc.getElementById(button.getAttribute("aria-controls"));
    if (!input) return;
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    button.setAttribute("aria-pressed", show ? "true" : "false");
    button.setAttribute("aria-label", show ? "パスワードを隠す" : "パスワードを表示");
    setIcon(button.querySelector("svg"), show ? "eye-off" : "eye");
  });

  /* ===== Client-side list filters ===== */
  function applyTextFilter(input) {
    var container = doc.querySelector(input.getAttribute("data-filter-target"));
    if (!container) return;
    var query = input.value.trim().toLowerCase();
    var shown = 0;
    $all("[data-search]", container).forEach(function (item) {
      var match = !query || item.getAttribute("data-search").toLowerCase().indexOf(query) !== -1;
      item.hidden = !match;
      if (match) shown += 1;
    });
    var empty = $("[data-filter-empty]", container.parentNode);
    if (empty) empty.hidden = shown !== 0;
    var counter = doc.querySelector(input.getAttribute("data-filter-count") || "");
    if (counter) counter.textContent = String(shown);
  }

  doc.addEventListener("input", function (event) {
    if (event.target.matches && event.target.matches("[data-filter-target]")) applyTextFilter(event.target);
  });

  doc.addEventListener("click", function (event) {
    var button = event.target.closest("[data-seg-filter] button[data-value]");
    if (!button) return;
    var group = button.closest("[data-seg-filter]");
    var target = doc.querySelector(group.getAttribute("data-seg-filter"));
    var value = button.getAttribute("data-value");
    $all("button[data-value]", group).forEach(function (other) {
      other.setAttribute("aria-pressed", other === button ? "true" : "false");
    });
    if (!target) return;
    var shown = 0;
    $all("[data-kind]", target).forEach(function (item) {
      var match = value === "all" || item.getAttribute("data-kind") === value;
      item.hidden = !match;
      if (match) shown += 1;
    });
    var empty = $("[data-filter-empty]", target.parentNode);
    if (empty) empty.hidden = shown !== 0;
  });

  /* ===== Init ===== */
  function init() {
    initTheme();
    initDialogs(doc);
    initTabs(doc);
    initDirtyForms();
    $all("[data-toast]").forEach(armToast);
    $all("button[type='submit']").forEach(syncAckButton);
    $all("[data-poll]").forEach(function (el) {
      el.setAttribute("data-poll-at", String(Date.now()));
    });
    var focusTarget = $("[data-scroll-into-view]");
    if (focusTarget) focusTarget.scrollIntoView({ block: "start" });
    tickElapsed();
  }

  doc.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (target && target.hasAttribute && target.hasAttribute("data-poll")) {
      target.setAttribute("data-poll-at", String(Date.now()));
    }
    initTabs(doc);
    tickElapsed();
  });

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();

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

  function isPoll(elt) {
    return !!(elt && elt.hasAttribute && elt.hasAttribute("data-poll"));
  }

  function pollBody(elt) {
    var target = elt.getAttribute("hx-target") || "";
    if (target.charAt(0) === "#") return doc.getElementById(target.slice(1));
    return elt;
  }

  /* 入力中・確認ダイアログ・操作メニューを、定期更新で消さない */
  function interactionBlocked(elt) {
    if (doc.documentElement.hasAttribute("data-order-pending")) return true;
    if (doc.querySelector("dialog[open]")) return true;
    if (openMenuState) return true;
    if (sidebar && sidebar.classList.contains("is-open")) return true;
    var body = pollBody(elt);
    var active = doc.activeElement;
    if (
      active &&
      body &&
      body.contains(active) &&
      active.matches &&
      active.matches("input, textarea, select, [contenteditable='true']")
    ) {
      return true;
    }
    var selection = window.getSelection && window.getSelection();
    if (selection && !selection.isCollapsed && body && selection.anchorNode && body.contains(selection.anchorNode)) return true;
    if (doc.querySelector("form.htmx-request, a.htmx-request, button.htmx-request")) return true;
    return false;
  }

  function captureLiveState(root) {
    if (!root) return null;
    return {
      scrollY: window.scrollY,
      filters: $all("[data-filter-target]", root).map(function (input) {
        return { id: input.id, value: input.value };
      }),
      segments: $all("[data-seg-filter]", root).map(function (group) {
        var pressed = $("button[aria-pressed='true']", group);
        return {
          target: group.getAttribute("data-seg-filter"),
          value: pressed ? pressed.getAttribute("data-value") : "all",
        };
      }),
      details: $all("details[open]", root).map(function (el) {
        var summary = $("summary", el);
        return summary ? summary.textContent : "";
      }),
    };
  }

  function restoreLiveState(root, state) {
    if (!root || !state) return;
    state.filters.forEach(function (item) {
      if (!item.id) return;
      var input = doc.getElementById(item.id);
      if (!input || !root.contains(input)) return;
      input.value = item.value;
      applyTextFilter(input);
    });
    state.segments.forEach(function (item) {
      var group = $all("[data-seg-filter]", root).filter(function (el) {
        return el.getAttribute("data-seg-filter") === item.target;
      })[0];
      if (!group) return;
      $all("button[data-value]", group).forEach(function (button) {
        button.setAttribute("aria-pressed", button.getAttribute("data-value") === item.value ? "true" : "false");
      });
      applySegFilter(group, item.value);
    });
    if (state.details.length) {
      $all("details", root).forEach(function (el) {
        var summary = $("summary", el);
        var text = summary ? summary.textContent : "";
        if (state.details.indexOf(text) !== -1) el.open = true;
      });
    }
    if (window.scrollY !== state.scrollY) window.scrollTo(0, state.scrollY);
  }

  var pollState = new WeakMap();
  var toggleState = new WeakMap();

  function focusMarker(el) {
    if (!el || el === doc.body || el === doc.documentElement || !el.getAttribute) return null;
    if (el.id) return { id: el.id };
    var href = el.getAttribute("href");
    if (href) return { href: href };
    var label = el.getAttribute("aria-label");
    if (label) return { label: label };
    return null;
  }

  function restoreFocus(root, marker) {
    if (!root || !marker) return;
    var active = doc.activeElement;
    if (active && active !== doc.body && active !== doc.documentElement && doc.contains(active)) return;
    var el = null;
    if (marker.id) {
      el = doc.getElementById(marker.id);
      if (el && !root.contains(el)) el = null;
    }
    if (!el && marker.href) {
      var links = $all("a[href]", root);
      for (var i = 0; i < links.length; i++) {
        if (links[i].getAttribute("href") === marker.href) {
          el = links[i];
          break;
        }
      }
    }
    if (!el && marker.label) {
      var nodes = $all("button, a", root);
      for (var j = 0; j < nodes.length; j++) {
        if (nodes[j].getAttribute("aria-label") === marker.label) {
          el = nodes[j];
          break;
        }
      }
    }
    if (el) el.focus({ preventScroll: true });
  }

  doc.addEventListener("htmx:configRequest", function (event) {
    var token = doc.querySelector('meta[name="csrf-token"]');
    if (token) event.detail.headers["X-CSRF-Token"] = token.content;
    var elt = event.detail.elt;
    if (!elt || !elt.hasAttribute) return;
    if (!elt.hasAttribute("data-poll") && !elt.hasAttribute("data-current-url")) return;
    /* 絞り込みやページ番号は、今のページを取り直す更新にだけ載せる。
       録画マークの /partials/recording-state まで差し替えると、ページ全体が
       hidden の中に入り、同名のダイアログがモーダルのまま画面に出なくなる。 */
    var requested = "";
    try {
      requested = new URL(event.detail.path, window.location.href).pathname;
    } catch (err) {
      return;
    }
    if (requested !== window.location.pathname) return;
    event.detail.path = window.location.pathname + window.location.search;
    var params = event.detail.parameters;
    if (params) {
      Object.keys(params).forEach(function (key) {
        delete params[key];
      });
    }
  });

  /* 一覧の差し替えはタブが裏のあいだ止める。録画マークだけは続ける。
     favicon は非アクティブなタブでも href の差し替えが反映される。 */
  function pausesWhileHidden(elt) {
    return elt.id !== "recording-state-poll";
  }

  doc.addEventListener("htmx:beforeRequest", function (event) {
    var elt = event.detail.elt;
    if (isPoll(elt)) {
      if ((doc.visibilityState === "hidden" && pausesWhileHidden(elt)) || interactionBlocked(elt)) event.preventDefault();
      return;
    }
    closeMenu(false);
  });

  doc.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    if (isLoginRedirect(xhr)) {
      event.detail.shouldSwap = false;
      window.location.assign("/login");
      return;
    }
    var source = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (isPoll(source)) {
      if (interactionBlocked(source)) {
        event.detail.shouldSwap = false;
      } else {
        var live = event.detail.target;
        var active = doc.activeElement;
        pollState.set(xhr, {
          id: live && live.id,
          state: captureLiveState(live),
          focus: live && active && live.contains(active) ? focusMarker(active) : null,
        });
      }
    }
    if (source && source.hasAttribute && source.hasAttribute("data-toggle") && xhr.status === 200) {
      var previous = source.querySelector("[role='switch']");
      toggleState.set(xhr, {
        rowId: source.getAttribute("data-row"),
        field: source.getAttribute("data-field"),
        label: source.getAttribute("data-label"),
        name: source.getAttribute("data-name"),
        wasOn: !!(previous && previous.getAttribute("aria-checked") === "true"),
      });
    }
  });

  /* 行の差し替えで新しいスイッチは最初から切り替わった状態になる。一度前の位置に戻してから滑らせる。 */
  function animateSwitch(sw, wasOn) {
    var next = sw.getAttribute("aria-checked") === "true";
    if (next === wasOn) return;
    var parts = sw.querySelectorAll(".c-switch__track, .c-switch__thumb");
    var state = sw.querySelector(".c-switch__state");
    parts.forEach(function (part) {
      part.style.transition = "none";
    });
    sw.setAttribute("aria-checked", wasOn ? "true" : "false");
    if (state) state.textContent = wasOn ? "ON" : "OFF";
    if (parts[0]) parts[0].getBoundingClientRect();
    parts.forEach(function (part) {
      part.style.transition = "";
    });
    sw.setAttribute("aria-checked", next ? "true" : "false");
    if (state) state.textContent = next ? "ON" : "OFF";
  }

  doc.addEventListener("htmx:afterSettle", function (event) {
    var request = event.detail.requestConfig && event.detail.requestConfig.elt;
    var xhr = event.detail.xhr;
    var info = xhr && toggleState.get(xhr);
    if (info) {
      toggleState.delete(xhr);
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
    if (!isPoll(request) && target && target.id === "results") {
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
    if (elt && elt.id === "recording-state-poll") {
      setRecordingMark("unknown");
      return;
    }
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
    $all("button[type='submit']", event.detail.elt).forEach(syncAckButton);
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
        var jump = $("[data-settings-jump]", container);
        if (jump && jump.value !== panelId) jump.value = panelId;
        if (useHash && !keepHash && window.history.replaceState) {
          window.history.replaceState(null, "", "#" + panelId);
        }
      }

      container._lrSelectTab = select;
      var jumpWrap = $(".c-settings-jump", container);
      if (jumpWrap) jumpWrap.hidden = false;
      var jumpControl = $("[data-settings-jump]", container);
      if (jumpControl && !jumpControl.hasAttribute("data-ready")) {
        jumpControl.setAttribute("data-ready", "");
        jumpControl.addEventListener("change", function () {
          var next = $('[aria-controls="' + jumpControl.value + '"]', container);
          if (next) select(next, true);
        });
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

  /* 非表示タブの入力は、ブラウザがフォーカスできず保存が黙って止まる。
     最初の不正項目のタブを開いてから、理由をその欄に出す。 */
  function fieldLabel(field) {
    if (!field.id) return "この項目";
    var label = doc.querySelector('label[for="' + field.id + '"]');
    if (!label) return "この項目";
    return label.textContent.replace(/\s+/g, " ").trim();
  }

  function fieldUnit(field) {
    var group = field.closest(".c-input-group");
    var addon = group && $(".c-input-group__addon", group);
    return addon ? addon.textContent.trim() : "";
  }

  function constraintMessage(field) {
    var label = fieldLabel(field);
    var validity = field.validity;
    var unit = fieldUnit(field);
    if (validity.valueMissing) return label + "を入力してください";
    if (validity.rangeUnderflow) return label + "は" + field.min + unit + "以上で入力してください";
    if (validity.rangeOverflow) return label + "は" + field.max + unit + "以下で入力してください";
    if (validity.patternMismatch) return label + "の形式が正しくありません";
    if (validity.stepMismatch) return label + "は指定された刻みで入力してください";
    if (validity.badInput || validity.typeMismatch) return label + "の値が正しくありません";
    return field.validationMessage || label + "を確認してください";
  }

  function showConstraintError(field) {
    var message = constraintMessage(field);
    field.setAttribute("aria-invalid", "true");
    if (!field.id) return message;
    var id = field.id + "-client-error";
    var existing = doc.getElementById(id);
    if (!existing) {
      existing = doc.createElement("p");
      existing.className = "c-field-error";
      existing.id = id;
      existing.setAttribute("role", "alert");
      var anchor = field.closest(".c-setting__control") || field.parentNode;
      anchor.appendChild(existing);
    }
    existing.textContent = message;
    var described = (field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    if (described.indexOf(id) === -1) {
      described.push(id);
      field.setAttribute("aria-describedby", described.join(" "));
    }
    return message;
  }

  function clearConstraintError(field) {
    if (!field.id) {
      field.removeAttribute("aria-invalid");
      return;
    }
    var id = field.id + "-client-error";
    var existing = doc.getElementById(id);
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
    var described = (field.getAttribute("aria-describedby") || "").split(/\s+/).filter(function (token) {
      return token && token !== id;
    });
    if (described.length) field.setAttribute("aria-describedby", described.join(" "));
    else field.removeAttribute("aria-describedby");
    field.removeAttribute("aria-invalid");
  }

  function initConstraintTabs(root) {
    $all("form[data-dirty-form]", root).forEach(function (form) {
      if (form.hasAttribute("data-constraint-ready")) return;
      form.setAttribute("data-constraint-ready", "");
      var pending = null;
      var reporting = false;
      form.addEventListener(
        "invalid",
        function (event) {
          if (reporting) return;
          var field = event.target;
          var panel = field.closest("[role='tabpanel']");
          var hidden = !!(panel && panel.hidden);
          if (hidden) event.preventDefault();
          if (pending) return;
          pending = { field: field, hidden: hidden };
          window.setTimeout(function () {
            var job = pending;
            pending = null;
            if (!job) return;
            var target = job.field;
            if (job.hidden && form._lrSelectTab) {
              var tab = $('[aria-controls="' + target.closest("[role='tabpanel']").id + '"]', form);
              if (tab) form._lrSelectTab(tab, false, false);
            }
            var message = showConstraintError(target);
            var statusText = $("[data-savebar-text]", form);
            if (statusText) statusText.textContent = message;
            target.scrollIntoView({ block: "center" });
            target.focus({ preventScroll: true });
            if (job.hidden && typeof target.reportValidity === "function") {
              reporting = true;
              try {
                target.reportValidity();
              } finally {
                reporting = false;
              }
            }
          }, 0);
        },
        true
      );
      form.addEventListener("input", function (event) {
        var field = event.target;
        if (field.checkValidity && field.checkValidity()) clearConstraintError(field);
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
      var fields = $all("input:not([type='hidden']), select:not([data-settings-jump]), textarea", form);
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
    var target = input.getAttribute("data-filter-target");
    var container = target ? doc.querySelector(target) : null;
    if (!container) return;
    var query = input.value.trim().toLowerCase();
    var shown = 0;
    $all("[data-search]", container).forEach(function (item) {
      var haystack = item.getAttribute("data-search") || "";
      var match = !query || haystack.toLowerCase().indexOf(query) !== -1;
      item.hidden = !match;
      if (match) shown += 1;
    });
    var empty = $("[data-filter-empty]", container.parentNode);
    if (empty) empty.hidden = shown !== 0;
    var countSelector = input.getAttribute("data-filter-count");
    var counter = countSelector ? doc.querySelector(countSelector) : null;
    if (counter) {
      var total = counter.getAttribute("data-total");
      counter.textContent = query && total ? shown + " / " + total : String(shown);
    }
  }

  doc.addEventListener("input", function (event) {
    if (event.target.matches && event.target.matches("[data-filter-target]")) applyTextFilter(event.target);
  });

  function applySegFilter(group, value) {
    var target = doc.querySelector(group.getAttribute("data-seg-filter"));
    $all("button[data-value]", group).forEach(function (other) {
      other.setAttribute("aria-pressed", other.getAttribute("data-value") === value ? "true" : "false");
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
  }

  doc.addEventListener("click", function (event) {
    var button = event.target.closest("[data-seg-filter] button[data-value]");
    if (!button) return;
    applySegFilter(button.closest("[data-seg-filter]"), button.getAttribute("data-value"));
  });

  function setRecordingMark(state) {
    ["app-mark", "app-mark-bar"].forEach(function (id) {
      var mark = doc.getElementById(id);
      if (!mark) return;
      mark.setAttribute("data-recording", state);
      mark.classList.toggle("is-recording", state === "on");
      mark.classList.toggle("is-unknown", state === "unknown");
      mark.setAttribute("aria-label", state === "on" ? "録画中" : state === "unknown" ? "状態不明" : "待機中");
    });
    syncFavicon();
  }

  function syncFavicon() {
    var link = doc.getElementById("favicon");
    var mark = doc.getElementById("app-mark");
    if (!link || !mark) return;
    var on = mark.getAttribute("data-recording") === "on";
    var next = link.getAttribute(on ? "data-recording" : "data-idle");
    if (next && link.getAttribute("href") !== next) link.setAttribute("href", next);
  }

  /* ===== Streamer reorder ===== */
  var reorderDrag = null;

  function reorderRows(body) {
    return $all("tr", body);
  }

  function reorderFiltered() {
    var filter = doc.getElementById("streamer-filter");
    return !!(filter && filter.value.trim());
  }

  function reorderIds(body) {
    return reorderRows(body).map(function (row) {
      return row.id.replace("streamer-", "");
    });
  }

  function dropPlace(body, y, ignore) {
    var rows = reorderRows(body).filter(function (row) {
      return row !== ignore && !row.hidden;
    });
    if (!rows.length) return null;
    for (var i = 0; i < rows.length; i++) {
      var rect = rows[i].getBoundingClientRect();
      var top = rect.top - rowShift(rows[i]);
      if (y < top + rect.height / 2) return { row: rows[i], before: true };
    }
    return { row: rows[rows.length - 1], before: false };
  }

  function moveRow(row, place) {
    if (!place) return;
    if (place.before) {
      if (place.row.previousElementSibling !== row) place.row.before(row);
    } else if (place.row.nextElementSibling !== row) {
      place.row.after(row);
    }
  }

  function rowShift(row) {
    var value = window.getComputedStyle(row).transform;
    if (!value || value === "none") return 0;
    return new DOMMatrixReadOnly(value).m42;
  }

  /* 入れ替え前後の見た目の位置を繋ぎ、行が隙間へ滑るようにする */
  function shiftRows(body, move) {
    var rows = reorderRows(body);
    var from = rows.map(function (row) {
      return row.offsetTop + rowShift(row);
    });
    move();
    var moved = [];
    rows.forEach(function (row, index) {
      var dy = from[index] - row.offsetTop;
      if (Math.abs(dy) < 0.5) {
        row.style.transform = "";
        row.style.transition = "";
        return;
      }
      row.style.transition = "none";
      row.style.transform = "translateY(" + dy + "px)";
      moved.push(row);
    });
    if (moved[0]) moved[0].getBoundingClientRect();
    moved.forEach(function (row) {
      row.style.transition = "";
      row.style.transform = "";
    });
  }

  function clearRowMotion(body) {
    reorderRows(body).forEach(function (row) {
      row.style.transform = "";
      row.style.transition = "";
    });
  }

  function liftRow(state, event) {
    var row = state.row;
    var rect = row.getBoundingClientRect();
    var source = row.closest("table");
    var floatTable = doc.createElement("table");
    var clone = row.cloneNode(true);
    var body = doc.createElement("tbody");
    floatTable.className = source.className + " c-row-float";
    floatTable.setAttribute("aria-hidden", "true");
    floatTable.inert = true;
    clone.removeAttribute("id");
    clone.querySelectorAll("[id]").forEach(function (node) {
      node.removeAttribute("id");
    });
    body.appendChild(clone);
    floatTable.appendChild(body);
    doc.body.appendChild(floatTable);
    floatTable.style.width = rect.width + "px";
    if (window.getComputedStyle(row).display === "table-row") {
      floatTable.style.tableLayout = "fixed";
      Array.prototype.forEach.call(row.children, function (cell, index) {
        var copy = clone.children[index];
        var width = cell.getBoundingClientRect().width;
        copy.style.width = width + "px";
        copy.style.maxWidth = "none";
      });
    }
    state.float = floatTable;
    state.offsetX = event.clientX - rect.left;
    state.offsetY = event.clientY - rect.top;
    placeFloat(state, event.clientX, event.clientY, false);
    row.classList.add("is-placeholder");
    doc.body.classList.add("is-reordering");
  }

  function placeFloat(state, x, y, animate) {
    var left = x - state.offsetX;
    var top = y - state.offsetY;
    var lift = animate ? "" : " scale(1.015)";
    state.float.style.transition = animate ? "transform var(--dur-base) var(--ease-out)" : "none";
    if (animate) state.float.getBoundingClientRect();
    state.float.style.transform = "translate3d(" + left + "px," + top + "px,0)" + lift;
  }

  function layoutPoint(row) {
    var rect = row.getBoundingClientRect();
    return { left: rect.left, top: rect.top - rowShift(row) };
  }

  function settleFloat(state, done) {
    var floatTable = state.float;
    if (!floatTable) {
      done();
      return;
    }
    var dest = layoutPoint(state.row);
    var finished = false;
    function finish(event) {
      if (finished) return;
      if (event && event.target !== floatTable) return;
      finished = true;
      floatTable.remove();
      state.float = null;
      done();
    }
    placeFloat(state, dest.left + state.offsetX, dest.top + state.offsetY, true);
    floatTable.addEventListener("transitionend", finish);
    window.setTimeout(finish, 280);
  }

  function visibleSibling(row, direction) {
    var node = row;
    while (node) {
      node = direction < 0 ? node.previousElementSibling : node.nextElementSibling;
      if (node && node.tagName === "TR" && !node.hidden) return node;
    }
    return null;
  }

  function announceReorder(row) {
    var status = doc.getElementById("reorder-status");
    var name = row.querySelector(".c-streamer__name");
    if (status && name) status.textContent = name.textContent.trim() + "の位置を変更しました";
  }

  var reorderSettling = false;
  var reorderSending = false;
  var reorderPendingIds = null;

  function refreshStreamers() {
    doc.documentElement.removeAttribute("data-order-pending");
    var poll = doc.getElementById("streamers-poll");
    if (poll && window.htmx) window.htmx.trigger(poll, "lr:refresh");
  }

  /* 保存は1本ずつ送る。失敗したときは、その後の操作が無ければサーバーの順序へ戻す。 */
  function pumpReorder() {
    if (reorderSending || !reorderPendingIds) return;
    var ids = reorderPendingIds;
    reorderPendingIds = null;
    reorderSending = true;
    doc.documentElement.setAttribute("data-order-pending", "");
    var token = (doc.querySelector('meta[name="csrf-token"]') || {}).content || "";
    var bodyText = ids
      .map(function (id) {
        return "id=" + encodeURIComponent(id);
      })
      .join("&");
    fetch("/streamers/reorder", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": token },
      body: bodyText,
    })
      .then(function (response) {
        reorderSending = false;
        if (reorderPendingIds) {
          pumpReorder();
          return;
        }
        if (response.ok) {
          doc.documentElement.removeAttribute("data-order-pending");
          return;
        }
        toast(
          response.status === 409
            ? "一覧が変わっていたため、並び順を保存できませんでした"
            : "並び順を保存できませんでした",
          "danger"
        );
        refreshStreamers();
      })
      .catch(function () {
        reorderSending = false;
        if (reorderPendingIds) {
          pumpReorder();
          return;
        }
        toast("並び順を保存できませんでした。ネットワークを確認してください", "danger");
        refreshStreamers();
      });
  }

  function saveReorder(body) {
    reorderPendingIds = reorderIds(body);
    doc.documentElement.setAttribute("data-order-pending", "");
    pumpReorder();
  }

  function focusReorderHandle(row) {
    var handle = row.querySelector("[data-reorder-handle]");
    if (!handle) return;
    function place() {
      if (doc.activeElement === handle) return;
      if (doc.activeElement && doc.activeElement !== doc.body && doc.activeElement !== doc.documentElement) return;
      handle.focus({ preventScroll: true });
    }
    handle.focus({ preventScroll: true });
    row.addEventListener("transitionend", function onEnd(event) {
      if (event.target !== row || event.propertyName !== "transform") return;
      row.removeEventListener("transitionend", onEnd);
      place();
    });
  }

  function endReorderDrag(commit) {
    var state = reorderDrag;
    if (!state) return;
    reorderDrag = null;
    if (state.loop) cancelAnimationFrame(state.loop);
    releaseReorderPointer(state.pointerId);
    doc.body.classList.remove("is-reordering");
    if (!state.active) {
      if (!reorderSending && !reorderPendingIds) doc.documentElement.removeAttribute("data-order-pending");
      return;
    }
    var changed = reorderRows(state.body).some(function (row, index) {
      return row !== state.origin[index];
    });
    var keep = commit && changed;
    reorderSettling = true;
    if (!keep && changed) {
      shiftRows(state.body, function () {
        state.origin.forEach(function (row) {
          state.body.appendChild(row);
        });
      });
    }
    settleFloat(state, function () {
      clearRowMotion(state.body);
      state.row.classList.remove("is-placeholder");
      reorderSettling = false;
      if (keep) {
        announceReorder(state.row);
        saveReorder(state.body);
        return;
      }
      if (!reorderSending && !reorderPendingIds) doc.documentElement.removeAttribute("data-order-pending");
    });
  }

  function dragFrame(state) {
    var edge = state.y < 72 ? -14 : state.y > window.innerHeight - 72 ? 14 : 0;
    if (edge) window.scrollBy(0, edge);
    if (!edge && state.y === state.placedY) return;
    state.placedY = state.y;
    var place = dropPlace(state.body, state.y, state.row);
    if (state.place && place && state.place.row === place.row && state.place.before === place.before) return;
    state.place = place;
    shiftRows(state.body, function () {
      moveRow(state.row, place);
    });
  }

  function ensureDragLoop(state) {
    if (state.loop) return;
    function tick() {
      if (reorderDrag !== state) {
        state.loop = 0;
        return;
      }
      dragFrame(state);
      state.loop = window.requestAnimationFrame(tick);
    }
    state.loop = window.requestAnimationFrame(tick);
  }

  doc.addEventListener("pointerdown", function (event) {
    if (reorderDrag || reorderSettling) return;
    var handle = event.target.closest && event.target.closest("[data-reorder-handle]");
    if (!handle || event.button !== 0) return;
    var row = handle.closest("tr");
    var body = row && row.parentElement;
    if (!row || !body || body.id !== "streamer-rows") return;
    if (reorderFiltered()) {
      toast("絞り込みを解除すると並べ替えできます", "warning");
      return;
    }
    event.preventDefault();
    doc.documentElement.setAttribute("data-order-pending", "");
    reorderDrag = {
      pointerId: event.pointerId,
      row: row,
      body: body,
      startX: event.clientX,
      startY: event.clientY,
      active: false,
      origin: reorderRows(body),
    };
    captureReorderPointer(event);
  });

  /* 持ち手は行と一緒に動く。そこへ捕捉すると、1行動かした瞬間に捕捉が外れてドラッグが終わる。 */
  function captureReorderPointer(event) {
    if (captureReorderPointer.busy) return;
    captureReorderPointer.busy = true;
    try {
      doc.documentElement.setPointerCapture(event.pointerId);
    } catch (err) {
      /* 合成イベントや未対応ブラウザでは捕捉できない */
    }
    captureReorderPointer.busy = false;
  }

  function releaseReorderPointer(pointerId) {
    try {
      if (doc.documentElement.hasPointerCapture(pointerId)) {
        doc.documentElement.releasePointerCapture(pointerId);
      }
    } catch (err) {
      /* すでに解放されている */
    }
  }

  doc.addEventListener(
    "pointermove",
    function (event) {
      if (!reorderDrag || event.pointerId !== reorderDrag.pointerId) return;
      var dx = event.clientX - reorderDrag.startX;
      var dy = event.clientY - reorderDrag.startY;
      if (!reorderDrag.active) {
        if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
        reorderDrag.active = true;
        liftRow(reorderDrag, event);
      }
      event.preventDefault();
      reorderDrag.x = event.clientX;
      reorderDrag.y = event.clientY;
      placeFloat(reorderDrag, event.clientX, event.clientY, false);
      captureReorderPointer(event);
      ensureDragLoop(reorderDrag);
    },
    { passive: false }
  );

  doc.addEventListener("pointerup", function (event) {
    if (!reorderDrag || event.pointerId !== reorderDrag.pointerId) return;
    endReorderDrag(true);
  });
  doc.addEventListener("pointercancel", function (event) {
    if (!reorderDrag || event.pointerId !== reorderDrag.pointerId) return;
    endReorderDrag(false);
  });
  doc.addEventListener("lostpointercapture", function (event) {
    if (!reorderDrag || event.pointerId !== reorderDrag.pointerId) return;
    if (event.buttons & 1) captureReorderPointer(event);
  });

  doc.addEventListener("keydown", function (event) {
    if (reorderDrag && event.key === "Escape") {
      event.preventDefault();
      endReorderDrag(false);
      return;
    }
    if (reorderSettling) return;
    var handle = event.target.closest && event.target.closest("[data-reorder-handle]");
    if (!handle) return;
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    var row = handle.closest("tr");
    var body = row && row.parentElement;
    if (!row || !body || body.id !== "streamer-rows") return;
    if (reorderFiltered()) {
      event.preventDefault();
      toast("絞り込みを解除すると並べ替えできます", "warning");
      return;
    }
    var sibling = visibleSibling(row, event.key === "ArrowUp" ? -1 : 1);
    if (!sibling) return;
    event.preventDefault();
    shiftRows(body, function () {
      if (event.key === "ArrowUp") sibling.before(row);
      else sibling.after(row);
    });
    focusReorderHandle(row);
    announceReorder(row);
    saveReorder(body);
  });

  /* ===== Init ===== */
  function init() {
    initTheme();
    initDialogs(doc);
    initTabs(doc);
    initConstraintTabs(doc);
    initDirtyForms();
    $all("[data-toast]").forEach(armToast);
    $all("button[type='submit']").forEach(syncAckButton);
    $all("[data-poll]").forEach(function (el) {
      el.setAttribute("data-poll-at", String(Date.now()));
    });
    var focusTarget = $("[data-scroll-into-view]");
    if (focusTarget) focusTarget.scrollIntoView({ block: "start" });
    tickElapsed();
    syncFavicon();
  }

  doc.addEventListener("htmx:afterSwap", function (event) {
    var source = event.detail.requestConfig && event.detail.requestConfig.elt;
    var xhr = event.detail.xhr;
    if (isPoll(source)) source.setAttribute("data-poll-at", String(Date.now()));
    var saved = xhr && pollState.get(xhr);
    if (isPoll(source) && saved && saved.id) {
      var restored = doc.getElementById(saved.id);
      restoreLiveState(restored, saved.state);
      restoreFocus(restored, saved.focus);
    }
    if (xhr) pollState.delete(xhr);
    var tog = xhr && toggleState.get(xhr);
    if (tog) {
      var row = doc.getElementById(tog.rowId);
      var sw = row && row.querySelector('[data-field="' + tog.field + '"] [role="switch"]');
      if (sw) animateSwitch(sw, tog.wasOn);
    }
    var target = event.detail.target;
    var current = (target && target.id && doc.getElementById(target.id)) || target;
    if (current) initDialogs(current);
    initTabs(doc);
    tickElapsed();
    syncFavicon();
  });

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();

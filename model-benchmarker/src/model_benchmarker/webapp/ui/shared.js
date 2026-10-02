(function () {
  "use strict";

  var KEY_STORAGE = "mb-api-key";

  function getKey() {
    try { return sessionStorage.getItem(KEY_STORAGE) || ""; } catch (e) { return ""; }
  }

  function setKey(k) {
    try { sessionStorage.setItem(KEY_STORAGE, k); } catch (e) { /* private mode */ }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // fetch wrapper: attaches X-API-Key, prompts once on 401, retries once
  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "X-API-Key": getKey() }, opts.headers || {});
    if (opts.body && !opts.headers["Content-Type"]) opts.headers["Content-Type"] = "application/json";
    return fetch(path, opts).then(function (resp) {
      if (resp.status === 401) {
        return promptForKey().then(function (k) {
          if (!k) throw new Error("API key required");
          setKey(k);
          opts.headers["X-API-Key"] = k;
          return fetch(path, opts).then(function (r2) { return finish(r2, path); });
        });
      }
      return finish(resp, path);
    });
  }

  function finish(resp, path) {
    return resp.text().then(function (text) {
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
      if (!resp.ok) {
        var detail = data && (data.detail || data.error) ? (data.detail || data.error) : resp.status + " " + resp.statusText;
        var err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        err.status = resp.status;
        err.data = data;
        throw err;
      }
      return data;
    });
  }

  function promptForKey() {
    return new Promise(function (resolve) {
      var existing = document.getElementById("mb-key-overlay");
      if (existing) existing.remove();
      var wrap = document.createElement("div");
      wrap.id = "mb-key-overlay";
      wrap.style.cssText = "position:fixed;inset:0;background:rgba(1,4,9,.82);display:flex;align-items:center;justify-content:center;z-index:99;";
      wrap.innerHTML =
        '<div class="panel" style="max-width:440px;width:92%">' +
        "<h1 style=margin-top:0>API key required</h1>" +
        '<p class="sub">Benchmark pages are gated by the deployment\'s API key ' +
        "(the value in the chart's Secret). The key stays in this tab only.</p>" +
        '<input id="mb-key-input" type="password" placeholder="X-API-Key" autocomplete="off" autofocus>' +
        '<div style="display:flex;gap:8px;margin-top:14px">' +
        '<button id="mb-key-save">Unlock</button>' +
        '<button id="mb-key-cancel" class="secondary">Cancel</button>' +
        "</div></div>";
      document.body.appendChild(wrap);
      var input = wrap.querySelector("#mb-key-input");
      input.focus();
      function submit() {
        var v = input.value.trim();
        wrap.remove();
        resolve(v);
      }
      wrap.querySelector("#mb-key-save").addEventListener("click", submit);
      wrap.querySelector("#mb-key-cancel").addEventListener("click", function () { wrap.remove(); resolve(""); });
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
    });
  }

  var LOGO_SVG_ATTRS = 'xmlns="http://www.w3.org/2000/svg" width="40" height="27" viewBox="0 0 432 288" style="flex-shrink:0"';

  function header(active) {
    var links = [
      ["/", "Memory estimator"],
      ["/benchmark/chat", "Chat benchmark"],
      ["/benchmark/endpoint", "Endpoint benchmark"],
      ["/results", "Results"],
    ];
    var nav = links
      .map(function (l) {
        return '<a href="' + l[0] + '"' + (l[0] === active ? ' class="active"' : "") + ">" + l[1] + "</a>";
      })
      .join("");
    var logo = document.getElementById("hpe-logo-template").innerHTML.replace("<svg ", "<svg " + LOGO_SVG_ATTRS + " ");
    var el = document.createElement("header");
    el.className = "hpe-header";
    el.innerHTML =
      '<div class="hpe-header-left" style="display:flex;align-items:center;gap:14px">' +
      '<div style="width:40px;flex-shrink:0">' + logo + "</div>" +
      '<div class="hpe-brand">' +
      '<div class="hpe-brand-name">Hewlett Packard Enterprise</div>' +
      '<div class="hpe-brand-title">Model <b>Benchmarker</b></div>' +
      "</div></div>" +
      "<nav>" + nav + "</nav>" +
      '<div class="theme-toggle" onclick="MB.toggleTheme()" title="Toggle dark/light mode">' +
      '<span id="theme-label">Light</span>' +
      '<div class="theme-toggle-track"><div class="theme-toggle-thumb"></div></div>' +
      "</div>" +
      '<span class="chip" id="mb-status-chip" style="margin-left:12px">checking…</span>';
    document.body.insertBefore(el, document.body.firstChild);
    var t = localStorage.getItem("theme") || "light";
    if (t === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
      var lab = document.getElementById("theme-label");
      if (lab) lab.textContent = "Dark";
    }
    fetch("/api/status")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var chip = document.getElementById("mb-status-chip");
        if (!chip) return;
        var bits = ["v" + s.version];
        bits.push(s.auth_required ? "key-gated" : "auth open");
        if (s.telemetry) bits.push("GPU telemetry on");
        chip.textContent = bits.join(" · ");
        chip.title = JSON.stringify(s, null, 2);
      })
      .catch(function () {
        var chip = document.getElementById("mb-status-chip");
        if (chip) chip.textContent = "status unavailable";
      });
  }

  function toggleTheme() {
    var html = document.documentElement;
    var isDark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", isDark ? "light" : "dark");
    var lab = document.getElementById("theme-label");
    if (lab) lab.textContent = isDark ? "Light" : "Dark";
    localStorage.setItem("theme", isDark ? "light" : "dark");
  }

  function fmtBytes(n) {
    if (n == null) return "-";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KiB";
    return (n / 1048576).toFixed(2) + " MiB";
  }

  function fmtNum(n, digits) {
    if (n == null || isNaN(n)) return "-";
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits == null ? 1 : digits });
  }

  // artifact download: header-authenticated fetch -> blob save (the key never
  // appears in a URL, so it cannot leak into access logs or Referer headers)
  function downloadArtifact(runId, name) {
    return api("/api/runs/" + encodeURIComponent(runId) + "/artifacts/" + encodeURIComponent(name))
      .then(function () {
        // api() returns parsed JSON; re-fetch raw for the blob
        return fetch("/api/runs/" + encodeURIComponent(runId) + "/artifacts/" + encodeURIComponent(name), {
          headers: { "X-API-Key": getKey() },
        });
      })
      .then(function (resp) {
        if (!resp.ok) throw new Error("download failed (" + resp.status + ")");
        return resp.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
      });
  }

  // Searchable combobox (ModelDownloader-style): input + toggle + menu of
  // matches; free text ALWAYS wins -- an unmatched term just stays typed.
  function combobox(inputId, getSuggestions, renderMeta) {
    var input = document.getElementById(inputId);
    if (!input) return;
    var wrap = input.closest(".combo");
    if (!wrap) return;
    var menu = wrap.querySelector(".combo-menu");
    var toggle = wrap.querySelector(".combo-toggle");
    if (!menu || !toggle) return;

    function show(q) {
      var items = getSuggestions() || [];
      var ql = (q || "").toLowerCase();
      var matches = items
        .filter(function (t) {
          return t.url.toLowerCase().indexOf(ql) >= 0 || t.name.toLowerCase().indexOf(ql) >= 0;
        })
        .slice(0, 50);
      menu.innerHTML = "";
      if (!matches.length) {
        var empty = document.createElement("div");
        empty.className = "combo-empty";
        empty.textContent = "No suggested targets match \u2014 you can still type any URL manually.";
        menu.appendChild(empty);
      } else {
        matches.forEach(function (t) {
          var item = document.createElement("button");
          item.type = "button";
          item.className = "combo-item";
          item.innerHTML = '<span class="combo-item-id">' + esc(t.name) + "</span>" +
            '<span class="combo-item-name">' + esc(t.url) + "</span>";
          item.addEventListener("click", function () {
            input.value = t.url;
            menu.hidden = true;
            if (renderMeta) renderMeta(t);
          });
          menu.appendChild(item);
        });
      }
      menu.hidden = false;
    }

    toggle.addEventListener("click", function () {
      if (menu.hidden) show(input.value);
      else menu.hidden = true;
    });
    input.addEventListener("input", function () { show(input.value); });
    input.addEventListener("focus", function () { show(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape" || e.key === "Enter") menu.hidden = true;
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".combo")) menu.hidden = true;
    });
  }

  // Recent-runs restore: the run panel was tab-local, so leaving the page
  // "lost" a running benchmark. This re-attaches: lists the latest runs and
  // offers Open (loads it into the run panel + resumes polling) / Cancel.
  // Gated endpoint: without the key the section silently stays hidden.
  function recentRuns(listEl, onOpen) {
    var box = document.getElementById(listEl);
    if (!box) return;
    api("/api/runs")
      .then(function (d) {
        var runs = (d.runs || []).slice(0, 6);
        if (!runs.length) return;
        var badge = function (s) {
          var cls = s === "success" ? "ok" : s === "running" ? "run" : s === "cancelled" || s === "interrupted" ? "idle" : "bad";
          return '<span class="badge ' + cls + '">' + esc(s) + "</span>";
        };
        var rows = runs
          .map(function (r) {
            return (
              '<tr><td class="mono">' + esc(r.run_id.slice(0, 19)) + "</td>" +
              "<td>" + esc(r.kind) + "</td>" +
              '<td class="mono" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.target_url) + "</td>" +
              "<td>" + badge(r.status) + "</td>" +
              '<td><a href="#" data-open="' + esc(r.run_id) + '">open</a></td></tr>'
            );
          })
          .join("");
        box.innerHTML =
          '<div class="panel"><h2 style="margin-top:0">Recent runs</h2>' +
          '<table><thead><tr><th>Run</th><th>Kind</th><th>Target</th><th>Status</th><th></th></tr></thead>' +
          "<tbody>" + rows + "</tbody></table></div>";
        box.querySelectorAll("a[data-open]").forEach(function (a) {
          a.addEventListener("click", function (ev) {
            ev.preventDefault();
            onOpen(a.getAttribute("data-open"));
          });
        });
      })
      .catch(function () {
        /* no key (or none yet): keep the section hidden */
      });
  }

    // Public recent-results strip for the memory page: keyless (the runs read path
  // is public), last 5 runs, deep-links into the /results detail views.
  function recentResults(listEl, max) {
    var box = document.getElementById(listEl);
    if (!box) return;
    fetch("/api/results")
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("unavailable")); })
      .then(function (d) {
        var runs = (d.runs || []).slice(0, max || 5);
        if (!runs.length) { box.innerHTML = ""; return; }
        var badge = function (s) {
          var cls = s === "success" ? "ok" : s === "running" ? "run" : (s === "cancelled" || s === "interrupted") ? "idle" : "bad";
          return '<span class="badge ' + cls + '">' + esc(s) + "</span>";
        };
        var rows = runs
          .map(function (r) {
            var when = (r.started_utc || "").replace("T", " ").slice(0, 16);
            return (
              '<tr><td>' + esc(r.kind) + "</td>" +
              '<td class="mono" style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.target_url) + "</td>" +
              "<td>" + badge(r.status) + "</td>" +
              '<td class="muted">' + esc(when) + "</td>" +
              '<td><a href="/results?run=' + encodeURIComponent(r.run_id) + '">open</a></td></tr>'
            );
          })
          .join("");
        box.innerHTML =
          '<div class="panel"><h2 style="margin-top:0">Recent results</h2>' +
          '<table><thead><tr><th>Kind</th><th>Target</th><th>Status</th><th>Started</th><th></th></tr></thead>' +
          "<tbody>" + rows + "</tbody></table>" +
          '<p style="margin:8px 0 0"><a href="/results">All results \u2192</a></p></div>';
      })
      .catch(function () { box.innerHTML = ""; });
  }

  // --- run-log view state ------------------------------------------------------
  // renderRun() replaces the panel's innerHTML on every 3s poll, which snaps
  // the log back to the top and re-opens a collapsed <details>. MB.logView
  // captures that state BEFORE the swap and .apply() restores it AFTER; while
  // the viewer is parked at the bottom it stays pinned there (log-follower).
  function logView(runBoxId) {
    var box = document.getElementById(runBoxId);
    var det = box ? box.querySelector("details") : null;
    var pre = box ? box.querySelector("pre.log") : null;
    return {
      open: det ? det.open : true,
      expanded: !!(det && det.classList.contains("log-full")),
      atBottom: pre ? pre.scrollHeight - pre.scrollTop - pre.clientHeight < 8 : true,
      top: pre ? pre.scrollTop : 0,
      apply: function () {
        var b = document.getElementById(runBoxId);
        var d = b ? b.querySelector("details") : null;
        var p = b ? b.querySelector("pre.log") : null;
        if (d) { d.open = this.open; d.classList.toggle("log-full", this.expanded); }
        if (p) p.scrollTop = this.atBottom ? p.scrollHeight : this.top;
      },
    };
  }

  function setLogFull(runBoxId, on) {
    var box = document.getElementById(runBoxId);
    var det = box ? box.querySelector("details") : null;
    if (!det) return;
    det.classList.toggle("log-full", !!on);
    var btn = det.querySelector(".log-fs");
    if (btn) btn.textContent = on ? "\u2715 exit full screen" : "\u26F6 full screen";
  }

  // one-time wiring per page: delegated click on the full-screen button (the
  // click must not toggle the surrounding <summary>) + Escape exits.
  function wireLogFullscreen(runBoxId) {
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest("#" + runBoxId + " .log-fs");
      if (!btn) return;
      ev.preventDefault();
      ev.stopPropagation();
      var det = btn.closest("details");
      setLogFull(runBoxId, !det.classList.contains("log-full"));
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      var det = document.querySelector("#" + runBoxId + " details.log-full");
      if (det) setLogFull(runBoxId, false);
    });
  }

  window.MB = { recentRuns: recentRuns, recentResults: recentResults,
    combobox: combobox,
    api: api, esc: esc, header: header, getKey: getKey, setKey: setKey,
    fmtBytes: fmtBytes, fmtNum: fmtNum, downloadArtifact: downloadArtifact, toggleTheme: toggleTheme,
    logView: logView, setLogFull: setLogFull, wireLogFullscreen: wireLogFullscreen,
  };
})();

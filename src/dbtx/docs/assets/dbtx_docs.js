/* dbtx docs runtime overlay.
 *
 * Renders accumulated run data from dbtx_runtime.json into the dbt docs site,
 * as a panel immediately below the node's Description section.
 *
 * The docs site is an Angular 1 app with hash routing, so the node detail DOM
 * is torn down and rebuilt on every navigation. This script therefore does not
 * inject once: it watches for the Description section appearing and re-renders
 * against whatever node the current route names.
 */
(function () {
  "use strict";

  var SIDECAR = "dbtx_runtime.json";
  var SECTION_ID = "dbtx-runtime-section";
  var state = { nodes: {}, loaded: false, historyLimit: null, version: 0 };

  // dbt routes are "#!/<resource>/<unique_id>?section=...". The unique_id
  // contains dots but never a slash or a query delimiter.
  function currentUniqueId() {
    var m = /#!?\/[a-z_]+\/([^?&/]+)/.exec(window.location.hash || "");
    return m ? decodeURIComponent(m[1]) : null;
  }

  function labelClass(status) {
    switch (String(status || "").toLowerCase()) {
      case "success":
      case "pass":
        return "label-success";
      case "error":
      case "fail":
      case "runtime error":
        return "label-danger";
      case "warn":
        return "label-warning";
      case "skipped":
        return "label-default";
      default:
        return "label-info";
    }
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    var s = Number(seconds);
    if (!isFinite(s)) return "—";
    if (s < 1) return (s * 1000).toFixed(0) + " ms";
    if (s < 60) return s.toFixed(2) + " s";
    var m = Math.floor(s / 60);
    return m + "m " + (s - m * 60).toFixed(0) + "s";
  }

  function fmtTimestamp(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function row(label, value) {
    var tr = el("tr");
    var th = el("th", null, label);
    th.style.whiteSpace = "nowrap";
    th.style.paddingRight = "1.5em";
    th.style.fontWeight = "500";
    tr.appendChild(th);
    tr.appendChild(el("td", null, value));
    return tr;
  }

  function renderLatest(body, run) {
    var head = el("div");
    head.style.marginBottom = "0.75em";
    var badge = el("span", "label " + labelClass(run.status), String(run.status));
    head.appendChild(badge);
    var timing = el("span", null, " in " + fmtDuration(run.execution_time));
    timing.style.marginLeft = "0.5em";
    head.appendChild(timing);
    body.appendChild(head);

    if (run.stale) {
      var warn = el(
        "div",
        "alert alert-warning",
        "This model's SQL has changed since this run. The timings below describe an earlier version of the node."
      );
      body.appendChild(warn);
    }

    var table = el("table", "table");
    table.style.marginBottom = "0";
    var tbody = el("tbody");
    tbody.appendChild(row("Last ran", fmtTimestamp(run.last_ran_at)));
    tbody.appendChild(row("Last compiled", fmtTimestamp(run.last_compiled_at)));
    // Job above invocation: the job id is the identifier a reader is likely to
    // recognise, the invocation id the one that uniquely pins the run.
    if (run.job_id) {
      tbody.appendChild(row("Job", run.job_id));
    }
    if (run.run_invocation_id) {
      tbody.appendChild(row("Invocation", run.run_invocation_id));
    }
    table.appendChild(tbody);
    body.appendChild(table);
  }

  function renderHistory(body, runs) {
    if (runs.length < 2) return;

    var heading = el("div", null, "Previous runs");
    heading.style.marginTop = "1.25em";
    heading.style.marginBottom = "0.5em";
    heading.style.fontWeight = "500";
    body.appendChild(heading);

    // Dropped entirely when nothing in the panel carries a job id, rather than
    // showing a column of dashes to projects that do not tag their runs. Keyed
    // on every run including the latest, so a project that starts tagging
    // mid-history keeps the column rather than having it appear only once a
    // previous run happens to be tagged.
    var prior = runs.slice(1);
    var showJob = runs.some(function (run) {
      return !!run.job_id;
    });

    var table = el("table", "table");
    table.style.marginBottom = "0";
    var thead = el("thead");
    var htr = el("tr");
    ["Status", "Duration", "Ran at"]
      .concat(showJob ? ["Job"] : [])
      .concat(["Invocation", ""])
      .forEach(function (h) {
        htr.appendChild(el("th", null, h));
      });
    thead.appendChild(htr);
    table.appendChild(thead);

    var tbody = el("tbody");
    prior.forEach(function (run) {
      var tr = el("tr");
      var statusCell = el("td");
      statusCell.appendChild(el("span", "label " + labelClass(run.status), String(run.status)));
      tr.appendChild(statusCell);
      tr.appendChild(el("td", null, fmtDuration(run.execution_time)));
      tr.appendChild(el("td", null, fmtTimestamp(run.last_ran_at)));

      // A run recorded before job ids were tracked, or one that passed no job
      // var, still occupies the column alongside runs that did.
      if (showJob) {
        var jobCell = el("td", null, run.job_id || "\u2014");
        jobCell.style.whiteSpace = "nowrap";
        tr.appendChild(jobCell);
      }

      // Matches the "Invocation" row the latest run shows, so a run can be
      // traced back to the dbt invocation that produced it.
      var invocationCell = el("td", null, run.run_invocation_id || "—");
      invocationCell.style.whiteSpace = "nowrap";
      tr.appendChild(invocationCell);

      var staleCell = el("td");
      if (run.stale) {
        var s = el("span", "label label-default", "stale");
        s.title = "The node's SQL has changed since this run";
        staleCell.appendChild(s);
      }
      tr.appendChild(staleCell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    body.appendChild(table);
  }

  function buildSection(uniqueId) {
    var section = el("section", "section");
    section.id = SECTION_ID;

    var target = el("div", "section-target");
    target.id = "dbtx-runtime";
    section.appendChild(target);

    var content = el("div", "section-content");
    content.appendChild(el("h6", null, "Run Results"));

    var panel = el("div", "panel");
    var body = el("div", "panel-body");

    var entry = state.nodes[uniqueId];
    var runs = entry && entry.runs ? entry.runs : [];

    if (!state.loaded) {
      var pending = el("div", "text-muted", "Loading run data…");
      body.appendChild(pending);
    } else if (!runs.length) {
      // Covers both nodes that have never been run and ephemeral models,
      // which never appear in run_results.json at all.
      body.appendChild(el("div", null, "No run data has been recorded for this node."));
    } else {
      renderLatest(body, runs[0]);
      renderHistory(body, runs);
    }

    panel.appendChild(body);
    content.appendChild(panel);
    section.appendChild(content);
    return section;
  }

  // What the panel's content depends on. The load state is part of it because
  // the first render usually happens while the sidecar fetch is still in
  // flight: that placeholder has to be replaced once the data lands, so
  // comparing the node alone would leave "Loading run data..." on screen
  // forever.
  function signature(uniqueId) {
    return uniqueId + "|" + (state.loaded ? "loaded" : "pending") + "|" + state.version;
  }

  // The Description section to sit under. There can be more than one in the
  // document during a router transition, and `getElementById` would hand back
  // whichever comes first -- possibly one in a view that is no longer on
  // screen. Prefer a section that is actually laid out.
  function descriptionSection() {
    var anchors = document.querySelectorAll('[id="description"]');
    var fallback = null;
    for (var i = 0; i < anchors.length; i++) {
      var section = anchors[i].closest ? anchors[i].closest("section.section") : null;
      if (!section || !section.parentNode) continue;
      if (!fallback) fallback = section;
      if (typeof section.getClientRects === "function" && section.getClientRects().length === 0) {
        continue;
      }
      return section;
    }
    // Nothing reported a box, which is also what a non-visual environment does.
    return fallback;
  }

  function render() {
    var hostSection = descriptionSection();
    if (!hostSection) return;

    var uniqueId = currentUniqueId();
    if (!uniqueId) return;

    var sig = signature(uniqueId);
    var existing = document.querySelectorAll("#" + SECTION_ID);

    // A panel already showing current content is left strictly alone -- not
    // moved, not rebuilt. Anything found here is attached to the document, so
    // it is on screen; relocating it risks pulling it out of the view being
    // read, and rebuilding it would briefly blank it for no gain. Position can
    // drift if the app re-renders around it; that is the cheaper problem.
    if (existing.length === 1 && existing[0].getAttribute("data-dbtx-sig") === sig) {
      return;
    }

    for (var i = 0; i < existing.length; i++) {
      if (existing[i].parentNode) existing[i].parentNode.removeChild(existing[i]);
    }

    var section = buildSection(uniqueId);
    section.setAttribute("data-dbtx-node", uniqueId);
    section.setAttribute("data-dbtx-sig", sig);
    hostSection.parentNode.insertBefore(section, hostSection.nextSibling);
  }

  var scheduled = false;
  function scheduleRender() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(function () {
      scheduled = false;
      try {
        render();
      } catch (e) {
        console.error("dbtx: render failed", e);
      }
    });
  }

  function load() {
    // Match the cache-busting the docs app uses for its own artifacts, so a
    // freshly patched sidecar is picked up on reload rather than served stale.
    return fetch(SIDECAR + "?cb=" + new Date().getTime())
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (dat) {
        state.nodes = dat.nodes || {};
        state.historyLimit = dat.history_limit || null;
        state.loaded = true;
        state.version++;
      })
      .catch(function (e) {
        // No sidecar yet is the normal pre-patch state, not an error worth
        // breaking the page over.
        state.loaded = true;
        state.version++;
        console.info("dbtx: no run data available (" + e.message + ")");
      });
  }

  function start() {
    // Stays connected for the life of the page. Disconnecting around our own
    // writes would drop any mutation the app made in that window, including one
    // that removed the panel, leaving nothing to trigger putting it back.
    new MutationObserver(scheduleRender).observe(document.body, {
      childList: true,
      subtree: true,
    });
    window.addEventListener("hashchange", scheduleRender);
    // The app's own bootstrap usually renders the node detail before this
    // resolves, so the placeholder goes up first and this pass replaces it.
    load().then(scheduleRender);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

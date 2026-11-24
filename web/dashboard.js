"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const paths = {
    layers: "M12 3 2 8l10 5 10-5-10-5ZM3 12l9 5 9-5M3 16l9 5 9-5",
    overview: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
    findings: "M4 4h16v16H4zM8 8h8M8 12h5M8 16h7",
    meeting: "M3 5h13v9H8l-4 3v-3H3zM17 9h4v11l-5-3h-5",
    review:
      "M7 3v12M17 6v2a4 4 0 0 1-4 4H7M4 18a3 3 0 1 0 6 0 3 3 0 0 0-6 0ZM14 5a3 3 0 1 0 6 0 3 3 0 0 0-6 0Z",
    code: "m8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18",
    eval: "M3 3v17h18M7 15l4-5 3 2 6-7",
    sources:
      "M4 6c0-4 16-4 16 0s-16 4-16 0Zm0 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0",
    repo: "M4 5a2 2 0 0 1 2-2h13v17H6a2 2 0 0 1-2-2V5Zm0 12a2 2 0 0 1 2-2h13M8 7h6",
    menu: "M3 6h18M3 12h18M3 18h18",
    close: "m6 6 12 12M18 6 6 18",
    panel: "M3 4h18v16H3zM9 4v16",
    terminal: "m4 7 5 5-5 5m8 0h7",
    refresh:
      "M20 7v5h-5M4 17v-5h5M5 9a7.5 7.5 0 0 1 13-3l2 3M4 15l2 3a7.5 7.5 0 0 0 13-3",
    check: "m5 12 4 4L19 6",
    circlecheck: "M21 12a9 9 0 1 1-3-7M8 11l4 4 8-9",
    alert: "m12 3 10 17H2L12 3Zm0 6v4m0 3v.3",
    file: "M5 2h9l5 5v15H5zM14 2v6h5M8 12h8M8 16h6",
    activity: "M2 12h5l3-8 4 16 3-8h5",
    edit: "m4 16 12-12 4 4L8 20H4v-4ZM14 6l4 4",
    clock: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 6v6l4 2",
    flask:
      "M8 3h8M10 3v6L4 19c-.5 1 .3 2 1.5 2h13c1.2 0 2-1 1.5-2L14 9V3M8 14h8",
    link: "m9 15 6-6m-5-2 2-2a5 5 0 0 1 7 7l-2 2m-3 3-2 2a5 5 0 0 1-7-7l2-2",
    folder: "M3 6h7l2 2h9v12H3z",
    download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
    bolt: "M13 2 5 14h6l-1 8 9-13h-7z",
    person: "M15 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM3 21v-2c0-6 16-6 16 0v2",
  };
  const icon = (name) =>
    `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.file}"/></svg>`;
  document
    .querySelectorAll("[data-icon]")
    .forEach((n) => (n.innerHTML = icon(n.dataset.icon)));
  const e = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const human = (s) =>
    String(s || "")
      .replace(/[-_]+/g, " ")
      .replace(/^./, (t) => t.toUpperCase());
  const rich = (s) => e(s).replace(/\x60([^\x60]+)\x60/g, "<code>$1</code>");
  const raw = $("snapshot").textContent.trim();
  const offline = raw && raw !== "null";
  let data = offline ? JSON.parse(raw) : null,
    running = false,
    lastView = "",
    search = "";
  const views = {
    overview: [
      "Overview",
      "overview",
      "Workspace overview",
      "Documentation health, code evidence, and meeting follow-ups.",
    ],
    findings: [
      "Audit findings",
      "findings",
      "Audit findings",
      "Inspect each claim against its implementation and see the suggested fix.",
    ],
    meetings: [
      "Meeting decisions",
      "meeting",
      "Meeting decisions",
      "Follow technical decisions from meeting notes into the code and documentation.",
    ],
    reviews: [
      "Reviews & releases",
      "review",
      "Reviews & releases",
      "Inspect proposed edits, review ownership, conflicts, and release wording.",
    ],
    workbench: [
      "API workbench",
      "code",
      "API workbench",
      "Explore documented endpoints and the checks attached to each claim.",
    ],
    evaluations: [
      "Evaluations",
      "eval",
      "Verifier evaluations",
      "Regression results from the project’s hand-labeled test cases.",
    ],
    sources: [
      "Data sources",
      "sources",
      "Data sources",
      "Inspect the files and integrations behind this workspace.",
    ],
  };
  const labels = {
    ready: "Ready for review",
    pending: "Pending",
    verified: "Verified",
    contradicted: "Contradicted",
    uncertain: "Uncertain",
    "manual review": "Manual review",
    "conflicting decisions": "Conflict",
  };
  const tones = {
    verified: "green",
    ready: "",
    pending: "amber",
    contradicted: "red",
    uncertain: "amber",
    "manual review": "amber",
    "conflicting decisions": "red",
    merged: "green",
    closed: "gray",
    open: "blue",
    draft: "",
  };
  const badge = (key, label) =>
    `<span class="badge ${tones[key] ?? "gray"}">${e(label || labels[key] || human(key))}</span>`;
  const tint = (key, tone = "") =>
    `<span class="tint ${tone}">${icon(key)}</span>`;
  const n = (key) => data.decision_counts[key] || 0;
  const fmt = (x) => (x == null ? "—" : String(x));
  const ratio = (v) => (v == null ? "—" : Math.round(v * 1000) / 10 + "%");
  const section = (title, after = "", desc = "") =>
    `<div class="sectionhead"><div><h2>${e(title)}</h2>${desc ? `<p>${e(desc)}</p>` : ""}</div>${after}</div>`;
  const link = (text, hash) =>
    `<a class="textlink" href="#${e(hash)}">${e(text)}</a>`;
  const source = (ico, title, body, status = "") =>
    `<div class="sourceitem">${tint(ico)}<div class="srctext"><strong>${e(title)}</strong><p>${e(body)}</p></div>${status ? `<span class="rowend">${status}</span>` : ""}</div>`;
  const empty = (s) => `<p class="empty">${e(s)}</p>`;
  const route = () => {
    const [key, qs = ""] = location.hash.slice(1).split("?");
    return {
      key: Object.hasOwn(views, key) ? key : "overview",
      query: new URLSearchParams(qs),
    };
  };
  const routeTo = (key, params = {}) =>
    key + (Object.keys(params).length ? "?" + new URLSearchParams(params) : "");
  const sortedDecisions = () =>
    [...data.decisions].sort(
      (a, b) =>
        rank(a.status) - rank(b.status) ||
        decisionTitle(a).localeCompare(decisionTitle(b)),
    );
  const rank = (s) =>
    ({
      "conflicting decisions": 0,
      ready: 1,
      pending: 2,
      "manual review": 3,
      verified: 4,
    })[s] ?? 5;
  const decisionTitle = (d) =>
    d.kind === "rename"
      ? `Rename ${d.old}`
      : d.kind === "change"
        ? `Change ${d.subject}`
        : `Remove ${d.old}`;
  function nav(view) {
    const groups = [
      ["Workspace", ["overview", "findings", "meetings", "reviews"]],
      ["Tools", ["workbench", "evaluations", "sources"]],
    ];
    $("nav").innerHTML = groups
      .map(
        ([group, keys]) =>
          `<div class="navgroup"><div class="navtitle">${group}</div>${keys.map((k) => `<a class="navitem" href="#${k}" ${view === k ? 'aria-current="page"' : ""}>${icon(views[k][1])}<span>${views[k][0]}</span>${k === "findings" && data ? `<span class="nbadge">${data.findings.length}</span>` : ""}</a>`).join("")}</div>`,
      )
      .join("");
  }
  function pagehead(view) {
    let action = "";
    if (view === "overview")
      action = offline
        ? `<a class="btn primary" href="#findings?verdict=contradicted">${icon("findings")}Explore findings</a>`
        : `<button class="btn primary" id="run-audit" ${running ? "disabled" : ""}>${icon("refresh")}${running ? "Running…" : "Run audit"}</button>`;
    if (view === "workbench" && !offline)
      action = `<a class="btn primary" href="/workbench">${icon("code")}Check your inputs</a>`;
    return `<div class="pagehead"><div><span class="context">${icon(data.demo ? "flask" : "repo")}${data.demo ? "Demo workspace" : "Repository workspace"}</span><h1>${views[view][2]}</h1><p>${views[view][3]}</p></div>${action}</div>`;
  }
  function hero() {
    const h = data.health;
    return `<section class="hero" aria-label="Repository health"><div class="herotext"><div class="eyebrow">${icon("activity")}Audit snapshot</div><h2>Documentation health</h2><div class="score"><strong>${fmt(h.score)}</strong><span>/ 100</span></div><p>${h.verified} of ${data.findings.length} documentation checks verified.</p></div><div class="art" aria-hidden="true"><div class="sheet"></div><div class="sheet mid"></div><div class="sheet front"><i></i><i></i><i></i><span class="seal">${icon("check")}</span></div></div></section>`;
  }
  function metric(label, count, ico, tone, url) {
    return `<a class="metric" style="color:inherit;text-decoration:none" href="#${e(url)}"><div class="metrictop">${tint(ico, tone)}${label}</div><strong>${count}</strong></a>`;
  }
  function fileChart() {
    const rows = Object.entries(data.health.files).sort(
      (a, b) => (a[1].score ?? 100) - (b[1].score ?? 100),
    );
    return `<section class="panel">${section("Health by document", "", "Score and verdict mix for each document.")}<div class="legend"><span class="key"><i class="swatch"></i>Verified</span><span class="key"><i class="swatch bad"></i>Contradicted</span>${data.health.uncertain ? '<span class="key"><i class="swatch unsure"></i>Uncertain</span>' : ""}</div>${
      rows
        .slice(0, 5)
        .map(
          ([path, h]) =>
            `<a class="filebtn" style="color:inherit;text-decoration:none" href="#${e(routeTo("findings", { file: path }))}"><div class="filetop"><span>${e(path)}</span><b>${fmt(h.score)} / 100</b></div><div class="track" aria-hidden="true">${[
              "verified",
              "contradicted",
              "uncertain",
            ]
              .filter((k) => h[k])
              .map(
                (k) =>
                  `<i class="${k === "contradicted" ? "bad" : k === "uncertain" ? "unsure" : ""}" style="flex:${Number(h[k])}"></i>`,
              )
              .join("")}</div></a>`,
        )
        .join("") || empty("No files have checkable claims.")
    }</section>`;
  }
  function decisionRow(d) {
    return `<button class="rowbtn" data-decision="${e(d.id)}">${tint(d.kind === "rename" ? "review" : d.kind === "remove" ? "file" : "edit", d.status === "pending" ? "amber" : "")}<span class="rowcopy"><strong>${e(decisionTitle(d))}</strong><small>${e(d.edits[0]?.file || (d.kind === "change" ? d.old + " → " + d.new : d.kind === "rename" ? d.new : "Source file"))}</small></span>${badge(d.status)}</button>`;
  }
  function findingRow({ f, i }) {
    return `<button class="rowbtn" data-finding="${i}">${tint(f.verdict === "verified" ? "circlecheck" : f.verdict === "uncertain" ? "clock" : "alert", tones[f.verdict])}<span class="rowcopy"><strong>${rich(f.message)}</strong><small>${e(f.file)}:${e(f.line)} · ${e(human(f.check))}</small></span>${badge(f.verdict)}</button>`;
  }
  const indexed = () => data.findings.map((f, i) => ({ f, i }));
  function overview() {
    const h = data.health;
    const ds = sortedDecisions();
    const recent = [
      ...ds.filter((d) => d.status === "ready").slice(0, 1),
      ...ds.filter((d) => d.status === "pending").slice(0, 1),
      ...ds.filter((d) => d.status === "verified").slice(0, 1),
    ];
    const spots = [...new Set([...recent, ...ds])].slice(0, 3);
    return (
      hero() +
      `<div class="metrics">${metric("Verified checks", h.verified, "circlecheck", "green", "findings?verdict=verified")}${metric("Contradictions", h.contradicted, "alert", "red", "findings?verdict=contradicted")}${metric("Meeting decisions", ds.length, "meeting", "blue", "meetings")}${metric("Edits ready", n("ready"), "edit", "", "reviews")}</div><div class="grid2">${fileChart()}<section class="panel">${section("Meeting decisions", link("View all", "meetings"), "Decision status against the current code.")}${spots.map(decisionRow).join("") || empty("No meeting decisions imported.")}</section></div><section class="panel">${section("Findings to review", link("View findings", "findings?verdict=contradicted"))}${
        indexed()
          .filter(({ f }) => f.verdict === "contradicted")
          .slice(0, 4)
          .map(findingRow)
          .join("") || empty("No contradictions in this audit.")
      }</section>`
    );
  }
  function options(values, current) {
    return values
      .map(
        ([val, text]) =>
          `<option value="${e(val)}" ${val === current ? "selected" : ""}>${e(text)}</option>`,
      )
      .join("");
  }
  function findings(query) {
    const selected = query.get("verdict") || "",
      file = query.get("file") || "";
    return `<section class="panel">${section("Evidence explorer")}<div class="toolbar"><input id="search" type="search" aria-label="Search findings" placeholder="Search messages, files, or evidence" value="${e(search)}"><select id="verdict" aria-label="Filter by verdict">${options([["", "All verdicts"], ...["verified", "contradicted", "uncertain"].map((x) => [x, labels[x]])], selected)}</select><select id="docfilter" aria-label="Filter by document">${options([["", "All documents"], ...Object.keys(data.health.files).map((x) => [x, x])], file)}</select></div><div id="filtered"></div></section>`;
  }
  function showFiltered() {
    const word = search.toLowerCase(),
      verdict = $("verdict").value,
      doc = $("docfilter").value;
    const rows = indexed().filter(
      ({ f }) =>
        (!verdict || f.verdict === verdict) &&
        (!doc || f.file === doc) &&
        (!word ||
          [f.message, f.file, f.evidence, f.check, f.claim, f.subject]
            .join(" ")
            .toLowerCase()
            .includes(word)),
    );
    $("filtered").innerHTML =
      `<p class="listfoot" role="status">${rows.length} of ${data.findings.length} findings</p>` +
      (rows.map(findingRow).join("") ||
        empty("No findings match these filters."));
  }
  function meetings(query) {
    const selected = query.get("status") || "";
    const ds = sortedDecisions();
    const states = [...new Set(ds.map((d) => d.status))];
    return `<section class="panel">${section("Decision register")}<div class="toolbar">${[["", "All decisions"], ...states.map((s) => [s, labels[s] || human(s)])].map(([s, title]) => `<a class="chip" style="color:inherit" aria-current="${s === selected ? "true" : "false"}" href="#${e(routeTo("meetings", s ? { status: s } : {}))}">${e(title)} · ${s ? n(s) : ds.length}</a>`).join("")}</div>${
      ds
        .filter((d) => !selected || d.status === selected)
        .map(decisionRow)
        .join("") || empty("No decisions to show.")
    }</section>`;
  }
  function reviews() {
    const ready = sortedDecisions().filter((d) => d.status === "ready");
    const gh = data.github;
    const ghList = (rows, kind) =>
      rows
        .map(
          (p) =>
            `<a class="rowbtn" style="color:inherit;text-decoration:none" target="_blank" rel="noopener" href="https://github.com/${e(gh.repo)}/${kind}/${Number(p.number)}">${tint(kind === "pull" ? "review" : "alert")}<span class="rowcopy"><strong>${e(p.title)}</strong><small>#${p.number}${p.owners?.length ? " · " + e(p.owners.join(", ")) : ""}</small></span>${badge(p.status)}</a>`,
        )
        .join("");
    return `<div class="grid2"><section class="panel">${section("Ready for a draft PR", badge("ready", String(ready.length)))}${ready.map(decisionRow).join("") || empty("No edits are ready for review.")}</section><section class="panel">${section("Conflicting decisions", badge("gray", String(data.conflicts.length)))}${data.conflicts.map((c) => `<details><summary>${e(c.title)}</summary><pre>${e(c.description)}</pre></details>`).join("") || empty("No conflicting decisions were found in these sources.")}</section></div><section class="panel stack">${section("GitHub review activity")}${gh ? ghList(gh.pulls, "pull") + ghList(gh.issues, "issues") || empty("No meeting PRs or conflict issues in the selected repository.") : source("review", "GitHub activity is offline", data.github_error || "Draft PRs, requested reviewers, and conflict issues appear here when a repository is connected.")}</section><section class="panel stack">${section("Release notes")}${data.release ? `<h3>Imported release draft</h3><pre>${e(data.release)}</pre>` : `<p class="muted">Approved, merged edits qualify for the release draft. The wording below previews edits awaiting review.</p>${ready.map((d) => `<p class="statusbox">${rich(d.wording)}</p>`).join("") || empty("No release wording to preview.")}`}</section>`;
  }
  function workbench() {
    const fs = indexed().filter(({ f }) => "claim" in f);
    const groups = new Map();
    for (const row of fs) {
      const claim = row.f.claim || "API documentation";
      if (!groups.has(claim)) groups.set(claim, []);
      groups.get(claim).push(row);
    }
    return `<section class="panel">${section("Documented API claims", badge("gray", String(fs.length) + " checks"))}${offline ? '<p class="muted">Explore the saved audit here. The local workbench also accepts your own OpenAPI spec, docs, and meeting notes.</p>' : ""}${[...groups].map(([name, rows], i) => `<details class="subpanel" ${i === 0 ? "open" : ""}><summary><b>${e(name)}</b> · ${rows.length} ${rows.length === 1 ? "check" : "checks"}</summary>${rows.map(findingRow).join("")}</details>`).join("") || empty("No API claims found.")}</section>`;
  }
  function evaluations() {
    const v = data.evaluation,
      passed = v.results.filter((c) => !c.problems.length).length;
    return `<section class="panel">${section("Labeled evaluation set", badge(passed === v.cases ? "verified" : "contradicted", passed + " / " + v.cases + " passed"), "Precision and recall against the project’s own fixtures.")}<table class="benchmark"><thead><tr><th>Verdict</th><th>Matches</th><th>Precision</th><th>Recall</th></tr></thead><tbody>${Object.entries(
      v.verdicts,
    )
      .map(
        ([k, t]) =>
          `<tr><td>${badge(k)}</td><td>${t.tp}</td><td>${ratio(t.precision)}</td><td>${ratio(t.recall)}</td></tr>`,
      )
      .join(
        "",
      )}</tbody></table></section><section class="panel stack">${section("Evaluation cases")}<div class="cases">${v.results.map((c) => `<div class="case"><div>${e(human(c.name))}${c.problems.map((p) => `<p>${e(p)}</p>`).join("")}</div>${badge(c.problems.length ? "contradicted" : "verified", c.problems.length ? "Failed" : "Passed")}</div>`).join("")}</div></section>`;
  }
  function sources() {
    const s = data.inputs;
    return `<div class="grid2"><section class="panel">${section("Workspace inputs", offline ? "" : '<a class="textlink" href="/portfolio.html" download="doc-check-portfolio.html">Download portfolio demo</a>')}${source("repo", data.project, s.api || "Repository source tree")}${source("folder", "Documentation", s.documents.join(", ") || "No documents")}${source("meeting", "Meeting exports", s.meetings.join(", ") || "No local meeting files selected")}${source("sources", "Saved decisions", s.state || "No persistent decision database selected")}</section><section class="panel">${section("Integration inputs")}${source("meeting", "Microsoft Teams", "Single meeting, organizer, or tenant collection through the meeting sync workflow.")}${source("file", "OneDrive & Loop", "Native Loop notes, Word transcripts, Markdown, text, and WebVTT.")}${source("review", "GitHub", data.github ? data.github.repo : data.github_error || "No live repository selected for this dashboard.")}${source(
      "person",
      "Documentation owners",
      Object.entries(s.owners)
        .map(([p, o]) => p + " · " + (o.join(", ") || "unassigned"))
        .join("; ") || "No documentation owners configured.",
    )}</section></div>`;
  }
  function detail(type, body) {
    $("detail-type").textContent = type;
    $("detail-body").innerHTML = body;
    $("detail").showModal();
    $("close-detail").focus();
  }
  function openFinding(i) {
    const f = data.findings[i];
    if (!f) return;
    detail(
      "Finding evidence",
      `${badge(f.verdict)}<h2>${rich(f.claim || f.subject || human(f.check))}</h2><p>${rich(f.message)}</p><h3>Documentation</h3><pre>${e(f.file)}:${e(f.line)}</pre><h3>Repository evidence</h3><pre>${e(f.evidence || "No supporting evidence recorded.")}</pre>${f.fix ? `<h3>Suggested fix</h3><p>${rich(f.fix)}</p>` : ""}`,
    );
  }
  function openDecision(id) {
    const d = data.decisions.find((i) => i.id === id);
    if (!d) return;
    detail(
      "Meeting decision",
      `${badge(d.status)}<h2>${e(decisionTitle(d))}</h2><p>${rich(d.wording)}</p>${d.sources.map((s) => `<h3>Meeting evidence</h3><p class="statusbox">${rich(s.quote)}</p><small class="muted">${e([s.file, s.time, s.speaker].filter(Boolean).join(" · "))}</small>`).join("")}<h3>Repository evidence</h3><pre>${e(d.evidence || "Awaiting repository evidence.")}</pre>${d.edits.length ? `<h3>Proposed documentation edits</h3>${d.edits.map((t) => `<div class="diff"><div class="path">${e(t.file)}:${t.line}</div><div class="minus">− ${e(t.before)}</div><div class="plus">+ ${e(t.after)}</div></div>`).join("")}<h3>Requested owners on publish</h3><p>${e(d.owners.join(", ") || "No owners configured for these documents.")}</p>` : ""}${d.status === "pending" ? '<p class="statusbox">The sync checks this decision again when the repository changes.</p>' : ""}`,
    );
  }
  function menu(open) {
    document.body.classList.toggle("navopen", open);
    $("menutoggle").setAttribute("aria-expanded", String(open));
    $("scrim").hidden = !open;
    if (open) $("nav").querySelector("[aria-current]")?.focus();
  }
  function render() {
    const { key, query } = route();
    nav(key);
    $("crumb").textContent = views[key][0];
    document.title = views[key][0] + " · Doc Check";
    if (!data) return;
    $("repo").textContent = data.project;
    $("side-mode").textContent = offline
      ? "Portfolio snapshot"
      : "Local workspace";
    if (key !== lastView) {
      search = "";
      window.scrollTo(0, 0);
    }
    lastView = key;
    const content = {
      overview,
      findings,
      meetings,
      reviews,
      workbench,
      evaluations,
      sources,
    }[key](query);
    $("content").innerHTML =
      pagehead(key) +
      content +
      (key === "overview" && data.notes.length
        ? `<div class="notice">${data.notes.map(e).join("<br>")}</div>`
        : "");
    if (key === "findings") {
      showFiltered();
      $("search").oninput = (ev) => {
        search = ev.target.value;
        showFiltered();
      };
      for (const id of ["verdict", "docfilter"])
        $(id).onchange = () => {
          const params = {};
          if ($("verdict").value) params.verdict = $("verdict").value;
          if ($("docfilter").value) params.file = $("docfilter").value;
          location.hash = routeTo("findings", params);
        };
    }
  }
  async function refresh() {
    if (running || offline) return;
    running = true;
    render();
    $("announce").textContent = "Running repository audit.";
    try {
      const response = await fetch("/api/workspace", { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok)
        throw new Error(payload.error || "Could not load the audit.");
      data = payload;
      render();
      $("announce").textContent = "Repository audit complete.";
    } catch (err) {
      $("content").innerHTML =
        `<h1>Workspace unavailable</h1><p class="notice error" role="alert">${e(err.message)}</p><button class="btn" id="retry">Retry audit</button>`;
    } finally {
      running = false;
      const b = $("run-audit");
      if (b) {
        b.disabled = false;
        b.innerHTML = icon("refresh") + "Run audit";
      }
    }
  }
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("button,a");
    if (!t) return;
    if (t.id === "run-audit" || t.id === "retry") refresh();
    else if (t.hasAttribute("data-finding"))
      openFinding(Number(t.dataset.finding));
    else if (t.hasAttribute("data-decision")) openDecision(t.dataset.decision);
    else if (t.id === "close-detail") $("detail").close();
    else if (t.id === "menutoggle") menu(true);
    else if (t.id === "scrim") {
      menu(false);
      $("menutoggle").focus();
    } else if (
      t.closest("#nav") &&
      document.body.classList.contains("navopen")
    ) {
      menu(false);
      $("content").focus();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && document.body.classList.contains("navopen")) {
      menu(false);
      $("menutoggle").focus();
    }
  });
  window.addEventListener("hashchange", () => {
    render();
    $("content").focus({ preventScroll: true });
  });
  render();
  if (!offline) refresh();
})();

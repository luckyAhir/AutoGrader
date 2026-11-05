/* AutoGrader browser interface. Plain JavaScript: no build step, no dependencies.
   Every piece of program text (sources, inputs, outputs) is inserted as text, never as HTML. */
"use strict";

const main = document.getElementById("main");
const tooltip = document.getElementById("tooltip");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ================================================================ DOM helpers */
function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  setAttrs(node, attrs);
  append(node, kids);
  return node;
}
function svg(tag, attrs, ...kids) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  setAttrs(node, attrs);
  append(node, kids);
  return node;
}
function setAttrs(node, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.setAttribute("class", v);
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
}
function fill(node, ...kids) {
  node.replaceChildren();
  append(node, kids);
  return node;
}
function append(node, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : String(kid));
  }
}

const ICON_PATHS = {
  check: ["M3.5 8.6l2.9 2.9 6.1-7"],
  cross: ["M4.5 4.5l7 7", "M11.5 4.5l-7 7"],
  alert: ["M8 2.2l6.3 11.3H1.7z", "M8 6.6v3.2", "M8 11.9v.1"],
  info: ["M8 1.8a6.2 6.2 0 1 0 0 12.4A6.2 6.2 0 0 0 8 1.8z", "M8 7.3v4.2", "M8 4.9v.1"],
};
function icon(name, cls) {
  return svg("svg", { class: "icon" + (cls ? " " + cls : ""), viewBox: "0 0 16 16", "aria-hidden": "true" },
    ICON_PATHS[name].map((d) => svg("path", { d })));
}
const BADGE_ICON = { good: "check", bad: "cross", warn: "alert", info: "info" };
function badge(kind, text) {
  return el("span", { class: `badge ${kind}` }, icon(BADGE_ICON[kind]), text);
}

/* ================================================================ formatting */
const fmt = {
  pts(x) {
    const r = Math.round(x * 10) / 10;
    return Number.isInteger(r) ? String(r) : r.toFixed(1);
  },
  pct(x) { return `${Math.round(x * 100)}%`; },
  compact(n) {
    const short = (v, digits, suffix) => `${v.toFixed(digits).replace(/\.0$/, "")}${suffix}`;
    if (n >= 1e9) return short(n / 1e9, 1, "B");
    if (n >= 1e6) return short(n / 1e6, n >= 1e7 ? 0 : 1, "M");
    if (n >= 1e4) return short(n / 1e3, n >= 1e5 ? 0 : 1, "K");
    return n.toLocaleString("en-US");
  },
  full(n) { return Number(n).toLocaleString("en-US"); },
  title(id) { return String(id || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()); },
  clip(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; },
};
const VERDICT = {
  AC: "Passed", WA: "Wrong answer", PE: "Formatting differs", TLE: "Time limit exceeded",
  MLE: "Memory limit exceeded", RE: "Runtime error", OLE: "Output limit exceeded", ERR: "Could not run", OK: "Ran",
};
const FLAG_TEXT = {
  suspected_plagiarism: "Possible copying", suspected_online_or_llm_copy: "Matches an online or LLM solution",
  suspected_infinite_loop: "Infinite loop suspected", memory_leak: "Memory leak", compile_error: "Does not compile",
  llm_unavailable: "LLM offline: style metrics used", llm_style_markers: "LLM-style comments",
  nondeterministic_output: "Output changes between runs", metric_not_evaluated: "Metric not evaluated here",
  analyzer_error: "Analyzer error", samples_only_tests: "Graded on the samples only",
  input_spec_ignored: "Input spec not usable", smoke_run_failed: "Sample run failed", no_input_read: "Never reads input",
  no_output: "Never prints",
};
/* Analyzer text marks code with backticks; render those spans as <code>. */
function rich(text) {
  return String(text || "").split(/(`[^`]+`)/).map((part) =>
    part.length > 2 && part.startsWith("`") && part.endsWith("`") ? el("code", { class: "inline" }, part.slice(1, -1)) : part);
}
const flagKind = (f) => (f.severity === "high" ? "bad" : f.severity === "medium" ? "warn" : "info");
const flagLabel = (f) => FLAG_TEXT[f.type] || fmt.title(f.type);

/* ================================================================ server access */
async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new Error("Cannot reach the AutoGrader server. Start it with `python -m autograder serve`, then reload this page.");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `The server answered ${res.status} ${res.statusText}.`);
  return body;
}
const post = (path, data) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });

const store = {
  get(key, fallback) {
    try { const v = JSON.parse(localStorage.getItem(`autograder.${key}`)); return v ?? fallback; } catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(`autograder.${key}`, JSON.stringify(value)); } catch { /* private mode */ } },
};

const state = { jobs: new Map(), stopPolling: null };

/* ================================================================ tooltip */
function showTip(html, x, y) {
  tooltip.replaceChildren(...html);
  tooltip.hidden = false;
  const pad = 14;
  const { width, height } = tooltip.getBoundingClientRect();
  let left = x + pad;
  let top = y + pad;
  if (left + width > window.innerWidth - 8) left = x - width - pad;
  if (top + height > window.innerHeight - 8) top = y - height - pad;
  tooltip.style.left = `${Math.max(8, left)}px`;
  tooltip.style.top = `${Math.max(8, top)}px`;
}
function hideTip() { tooltip.hidden = true; }
function tipOn(node, build) {
  node.addEventListener("pointermove", (e) => showTip(build(), e.clientX, e.clientY));
  node.addEventListener("pointerleave", hideTip);
}

/* ================================================================ router */
const ROUTES = [
  [/^#\/grade$/, "grade", () => renderGrade()],
  [/^#\/results(?:\/(.+))?$/, "results", (m) => renderResults(m[1] && decodeURIComponent(m[1]))],
  [/^#\/report\/job\/(\w+)$/, "grade", (m) => renderJobReport(m[1])],
  [/^#\/report\/file\/(.+)$/, "results", (m) => renderStoredReport(decodeURIComponent(m[1]))],
];

async function route(event) {
  if (state.stopPolling) { state.stopPolling(); state.stopPolling = null; }
  hideTip();
  const hash = location.hash || "#/grade";
  for (const [re, nav, render] of ROUTES) {
    const m = hash.match(re);
    if (!m) continue;
    document.querySelectorAll("[data-nav]").forEach((a) => {
      if (a.dataset.nav === nav) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    main.replaceChildren();
    window.scrollTo(0, 0);
    try {
      await render(m);
    } catch (err) {
      main.replaceChildren(errorSheet(err));
    }
    if (event) main.focus({ preventScroll: true });
    return;
  }
  location.replace("#/grade");
}

function errorSheet(err) {
  return el("section", { class: "sheet empty", role: "alert" },
    el("h2", {}, "This page could not be loaded"),
    el("p", {}, err.message || String(err)));
}

/* ================================================================ toolchain status (top bar) */
async function loadStatus() {
  const box = document.getElementById("toolstatus");
  let s;
  try { s = await api("/api/status"); } catch { box.replaceChildren(el("span", { class: "st no" }, icon("cross"), "Server offline")); return; }
  const item = (state, text, title) => el("span", { class: `st ${state}`, title }, icon(state === "ok" ? "check" : state === "half" ? "alert" : "cross"), text);
  const items = [el("span", { class: "st" }, s.compiler)];
  if (!s.ready) {
    items.push(el("span", { class: "st" }, "checking tools…"));
    setTimeout(loadStatus, 1500);
  } else {
    items.push(s.sanitizers === "asan" ? item("ok", "ASan + UBSan", "Memory errors, leaks and undefined behaviour are detected")
      : s.sanitizers ? item("half", "UBSan only", "No AddressSanitizer here: leaks and buffer overflows are not detected")
        : item("no", "No sanitizers", "Memory safety cannot be checked on this machine"));
    items.push(item(s.gcov ? "ok" : "half", s.gcov ? "gcov profiler" : "Timing only", s.gcov ? "Complexity uses exact operation counts" : "Complexity falls back to CPU timing"));
    items.push(s.llm.reachable ? item("ok", "LLM online", `Models: ${s.llm.models.join(", ")}`)
      : item("half", "LLM offline", "Readability uses cached LLM replies, or style metrics alone"));
  }
  box.replaceChildren(...items);
}

/* ================================================================ Grade page: two inputs, rules (with the statement) + program */
function debounce(fn, ms) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

function codeEditor({ id, label, value, placeholder, plain, onInput, onSubmit }) {
  const gutter = el("div", { class: "gutter", "aria-hidden": "true" });
  const area = el("textarea", { class: "code-input", id, wrap: "off", spellcheck: "false", autocapitalize: "off",
    "aria-label": label, placeholder });
  area.value = value || "";
  const renumber = () => {
    const n = Math.max(1, area.value.split("\n").length);
    if (gutter.dataset.n !== String(n)) {
      gutter.dataset.n = String(n);
      gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
    }
  };
  area.addEventListener("input", () => { renumber(); if (onInput) onInput(area.value); });
  area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });
  area.addEventListener("keydown", (e) => {
    if (onSubmit && (e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); onSubmit(); }
  });
  renumber();
  return {
    node: el("div", { class: `editor${plain ? " plain" : ""}` }, gutter, area),
    area,
    set(text) { area.value = text; renumber(); area.scrollTop = 0; gutter.scrollTop = 0; },
  };
}

function fileButton(label, accept, onText) {
  const input = el("input", { type: "file", accept, class: "visually-hidden", tabindex: "-1" });
  input.addEventListener("change", async () => {
    const file = input.files[0];
    input.value = "";
    if (file) onText(file, await file.text());
  });
  return [el("button", { type: "button", class: "btn", onclick: () => input.click() }, label), input];
}

function stepTitle(n, title, sub) {
  return el("div", { class: "step-title" },
    el("span", { class: "step-no", "aria-hidden": "true" }, n),
    el("div", {}, el("h2", {}, title), sub ? el("p", { class: "step-sub" }, sub) : null));
}

async function renderGrade() {
  let draft = store.get("draft", null);
  if (!draft || typeof draft.rules !== "string") draft = {};
  draft = { rules: draft.rules || "", filename: draft.filename || "submission.c",
    source: draft.source || "" };
  const save = debounce(() => store.set("draft", draft), 250);
  let summary = null;
  let checkSeq = 0;

  /* ---------- step 1: the rules */
  const status = el("div", { class: "rules-status", role: "status", "aria-live": "polite" });
  const rulesEd = codeEditor({
    id: "rules", label: "rules.json", plain: true, value: draft.rules,
    placeholder: "Paste the instructor's rules.json here, or open the file.",
    onInput: (text) => { draft.rules = text; save(); checkSoon(); },
  });
  const [openRulesBtn, openRulesInput] = fileButton("Open rules.json", ".json,application/json", (file, text) => {
    rulesEd.set(text);
    draft.rules = text;
    save();
    check();
  });

  /* ---------- step 2: the program */
  const filename = el("input", { class: "input", id: "filename", value: draft.filename, spellcheck: "false", autocomplete: "off" });
  const msg = el("p", { class: "form-msg", role: "status" });
  const gradeBtn = el("button", { type: "button", class: "btn btn-primary", disabled: true }, "Grade program");
  const progEd = codeEditor({
    id: "source", label: "C program", value: draft.source,
    placeholder: "Paste the student's C program here, or open the .c file.",
    onInput: (text) => { draft.source = text; save(); },
    onSubmit: () => gradeBtn.click(),
  });
  const [openCBtn, openCInput] = fileButton("Open .c file", ".c,text/x-c,text/plain", (file, text) => {
    if (file.size > 256 * 1024) { msg.textContent = `${file.name} is larger than 256 KB.`; return; }
    progEd.set(text);
    filename.value = file.name.endsWith(".c") ? file.name : `${file.name}.c`;
    Object.assign(draft, { source: text, filename: filename.value });
    msg.textContent = "";
    save();
  });
  filename.addEventListener("input", () => { draft.filename = filename.value; save(); });

  /* ---------- step 3: grade */
  const runlist = el("div", { class: "runlist" });
  const setBusy = (busy) => {
    gradeBtn.disabled = busy || !summary;
    gradeBtn.classList.toggle("busy", busy);
    gradeBtn.textContent = busy ? "Grading…" : "Grade program";
  };

  const showSummary = (s) => {
    const total = s.metrics.reduce((a, m) => a + m.weight, 0);
    fill(status,
      badge("good", "Ready to grade"),
      el("span", {}, `${fmt.title(s.problem)}: ${s.metrics.length} analyzers, weights add up to ${fmt.pct(total)}, ` +
        `${s.time_limit_s} s and ${s.memory_limit_mb} MB per run. Files go to ${s.folder}/.`),
      s.warnings.length ? el("p", { class: "muted small" }, `Warnings: ${s.warnings.join("; ")}`) : null);
    runlist.replaceChildren(runTable(s));
  };
  const showErrors = (errors) => {
    fill(status, badge("bad", "Fix these before grading"),
      el("ul", { class: "rule-errors" }, errors.map((e) => el("li", {}, e))));
    runlist.replaceChildren(el("p", { class: "muted" }, "The analyzers appear here once the rules are valid."));
  };
  const check = async () => {
    const seq = ++checkSeq;
    if (!draft.rules.trim()) {
      summary = null;
      fill(status, el("span", { class: "muted" }, "Paste or open a rules.json to begin."));
      runlist.replaceChildren();
      setBusy(false);
      return;
    }
    status.replaceChildren(el("span", { class: "muted" }, "Checking the rules…"));
    try {
      const r = await post("/api/rules/check", { rules: draft.rules });
      if (seq !== checkSeq) return;
      summary = r.ok ? r : null;
      if (r.ok) showSummary(r); else showErrors(r.errors);
    } catch (err) {
      if (seq !== checkSeq) return;
      summary = null;
      showErrors([err.message]);
    }
    setBusy(false);
  };
  const checkSoon = debounce(check, 450);

  gradeBtn.addEventListener("click", async () => {
    if (!summary) { msg.textContent = "Fix the rules (step 1) first."; return; }
    if (!progEd.area.value.trim()) {
      msg.textContent = "Paste a program or open a .c file in step 2 first.";
      progEd.area.focus();
      return;
    }
    msg.textContent = "";
    setBusy(true);
    const payload = { rules: draft.rules,
      filename: filename.value.trim() || "submission.c", source: progEd.area.value };
    try {
      const { id } = await post("/api/grade", payload);
      runlist.replaceChildren(runTable(summary, { jobId: id, filename: payload.filename, onFinish: (job) => {
        setBusy(false);
        if (job.state === "error") msg.textContent = `Grading stopped: ${job.error}`;
      } }));
      runlist.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
    } catch (err) {
      msg.textContent = err.message;
      setBusy(false);
    }
  });

  main.append(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, "Grade a program"),
        el("p", { class: "page-sub" }, "Two inputs: the rules (they hold the problem statement and the samples) and the C program. " +
          "The test cases are generated from them when you grade."))),
    el("div", { class: "inputs" },
      el("section", { class: "sheet", "aria-labelledby": "step1" },
        stepTitle(1, "Rules", "rules.json: the problem statement, sample tests, metrics, weights and limits"),
        el("div", { class: "editor-tools" }, openRulesBtn, openRulesInput),
        rulesEd.node,
        status),
      el("section", { class: "sheet", "aria-labelledby": "step2" },
        stepTitle(2, "C program", "The student's submission"),
        el("div", { class: "editor-tools" },
          openCBtn, openCInput,
          el("label", { class: "field push", for: "filename", title: "The file name identifies the student: grading the same name again replaces the earlier copy" }, "File name", filename)),
        progEd.node,
        el("p", { class: "hint", style: "margin-top:10px" }, "Ctrl+Enter grades. Both inputs are kept in this browser. The file name identifies the student; graded programs are kept in submissions/<problem>/ to check for copying."))),
    el("section", { class: "sheet grade-step" },
      el("div", { class: "grade-step-head" },
        stepTitle(3, "Grade", "Generates the test cases, then runs every analyzer in the rules"),
        el("div", { class: "grade-actions" }, gradeBtn)),
      msg, runlist));
  document.querySelectorAll(".step-title h2").forEach((h, i) => h.setAttribute("id", `step${i + 1}`));
  check();
}

/* The analyzers named by the rules; with a job it fills in live as each one finishes. */
function runTable(s, live) {
  const maxW = Math.max(...s.metrics.map((m) => m.weight));
  const rows = new Map();
  const items = s.metrics.map((m) => {
    const mark = el("span", { class: live ? "pending-dot" : "plain-dot", "aria-hidden": "true" });
    const res = el("span", { class: "mres" }, live ? "waiting" : "");
    const li = el("li", { class: live ? "waiting" : "" },
      mark,
      el("span", { class: "mname" }, fmt.title(m.name)),
      el("span", { class: "mdesc" }, el("code", {}, m.analyzer), ` ${m.description}${m.kind === "execution" ? " (runs the program)" : ""}`),
      el("span", { class: "mweight" },
        el("span", { class: "track", "aria-hidden": "true" },
          el("span", { class: "meter", style: `width:${(m.weight / maxW) * 100}%` }, el("span", { style: "width:100%" }))),
        el("span", { class: "pct" }, fmt.pct(m.weight))),
      res);
    rows.set(m.name, { li, mark, res, weight: m.weight });
    return li;
  });
  const list = el("ol", { class: "mlist", "aria-label": "Analyzers" }, items);
  if (!live) return el("div", {}, list);

  const prepMark = el("span", { class: "spinner", "aria-hidden": "true" });
  const prepText = el("span", { class: "mdesc" }, "Write the rules to problems/, then have the LLM design the test cases (can take a few minutes)");
  let fpMark = el("span", { class: "pending-dot", "aria-hidden": "true" });
  const fpText = el("span", { class: "mdesc" }, "Compile, check the source, run the sample input");
  const reportMark = el("span", { class: "pending-dot", "aria-hidden": "true" });
  const announce = el("p", { class: "visually-hidden", "aria-live": "polite" });
  let seen = 0;  // the server returns every event each time; handle only the new ones
  const update = (job) => {
    const fresh = job.events.slice(seen);
    seen = job.events.length;
    for (const ev of fresh) {
      if (ev.type === "generating") {
        prepText.textContent = ev.step === 0 ? `Test cases: ${ev.theme}`
          : `Designing test cases ${ev.step}/${ev.of} (${ev.kept} kept so far): ${fmt.clip(ev.theme, 60)}`;
      } else if (ev.type === "prepared") {
        const fine = ev.tests > 0 && !ev.errors.length;
        prepMark.replaceWith(icon(fine ? "check" : "alert", fine ? "ok" : "half"));
        fpMark.replaceWith(fpMark = el("span", { class: "spinner", "aria-hidden": "true" }));
        fill(prepText, fine ? `${ev.tests} test cases ready in ${ev.folder}/`
          : `Problem in ${ev.folder}/: ${ev.errors.join("; ") || "no test cases"}`);
      } else if (ev.type === "first_pass") {
        const fine = ev.compiled && !ev.suspected_infinite_loop;
        fpMark.replaceWith(icon(fine ? "check" : "cross", fine ? "ok" : "no"));
        fpText.textContent = !ev.compiled ? "Does not compile: the analyzers that run the program are skipped."
          : ev.suspected_infinite_loop ? "The sample run never finished (infinite loop suspected): the analyzers that run the program are skipped."
            : ev.smoke === "OK" ? "Compiled, and the sample input ran to completion."
              : `Compiled, but the sample run ended with: ${(VERDICT[ev.smoke] || ev.smoke || "").toLowerCase()}.`;
      } else if (ev.type === "metric") {
        const r = rows.get(ev.name);
        if (!r) continue;
        r.li.classList.remove("waiting");
        const skipped = ev.status !== "ok";
        const full = ev.score >= 0.999;
        r.mark.replaceWith(r.mark = icon(skipped ? "alert" : full ? "check" : "cross", skipped ? "half" : full ? "ok" : "no"));
        r.res.textContent = skipped ? "not counted" : `${fmt.pts(ev.score * r.weight * 100)} / ${fmt.pts(r.weight * 100)}`;
        r.res.title = ev.summary;
        announce.textContent = `${fmt.title(ev.name)} finished`;
      } else if (ev.type === "stage" && ev.stage === "collate") {
        reportMark.replaceWith(el("span", { class: "spinner", "aria-hidden": "true" }));
      }
    }
  };
  state.stopPolling = poll(live.jobId, (job) => {
    update(job);
    if (job.state === "done") {
      state.jobs.set(job.id, job);
      live.onFinish(job);
      location.hash = `#/report/job/${job.id}`;
    } else if (job.state === "error") {
      live.onFinish(job);
    }
  });
  return el("div", {},
    el("h3", { class: "run-head" }, `Grading ${live.filename}`),
    el("ol", { class: "mlist" },
      el("li", { class: "stage" }, prepMark, el("span", { class: "mname" }, "Reference & tests"), prepText),
      el("li", { class: "stage" }, fpMark, el("span", { class: "mname" }, "First pass"), fpText)),
    list,
    el("ol", { class: "mlist" }, el("li", { class: "stage" }, reportMark, el("span", { class: "mname" }, "Report"),
      el("span", { class: "mdesc" }, "Weighted grade, counterexamples and suggestions"))),
    announce);
}

function poll(jobId, onUpdate) {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (stopped) return;
      onUpdate(job);
      if (job.state === "done" || job.state === "error") return;
    } catch (err) {
      onUpdate({ state: "error", error: err.message, events: [] });
      return;
    }
    timer = setTimeout(tick, 350);
  };
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}

function choice(label, options, current, onPick) {
  return el("div", { class: "choice", role: "group", "aria-label": label },
    options.map((o) => el("button", {
      type: "button", "aria-pressed": o.value === current ? "true" : "false",
      onclick: () => { if (o.value !== current) onPick(o.value); },
    }, o.label)));
}

/* ================================================================ Report pages */
async function renderJobReport(id) {
  let job = state.jobs.get(id);
  if (!job) job = await api(`/api/jobs/${id}`);
  if (job.state !== "done") { location.replace("#/grade"); return; }
  reportView({ report: job.report, source: job.source, text: job.text,
    back: { href: "#/grade", label: "Back to the editor" } });
}

async function renderStoredReport(file) {
  const data = await api(`/api/report?file=${encodeURIComponent(file)}`);
  const group = file.split("/").slice(0, -1).join("/");
  reportView({ report: data.report, source: data.source, text: data.text,
    back: { href: `#/results/${encodeURIComponent(group)}`, label: "All results for this folder" } });
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function reportView({ report, source, text, back }) {
  const stem = report.submission.replace(/\.c$/, "");
  const notes = collectNotes(report);
  const script = scriptSheet(report, source, notes);
  main.append(
    el("div", { class: "report-head" },
      el("a", { class: "back", href: back.href }, back.label),
      el("div", {},
        el("h1", {}, report.submission),
        el("p", { class: "problem-name" }, `${fmt.title(report.problem)}, graded with ${report.rules_file}`)),
      el("div", { class: "report-actions" },
        el("button", { type: "button", class: "btn", onclick: () => download(`${stem}.json`, JSON.stringify(report, null, 2), "application/json") }, "Download JSON"),
        el("button", { type: "button", class: "btn", onclick: () => download(`${stem}.txt`, text || "", "text/plain") }, "Download text report"))),
    el("div", { class: "report-layout" }, script, markingSheet(report)));
  drawRing(script, report.grade / (report.max_grade || 100));
  countUp(script, report.grade);
}

/* ---------------------------------------------------------------- the marked script */
const SHORT_ISSUE = {
  "unchecked-input": "input not checked", "global-variable": "global variable", goto: "goto",
  "void-main": "main must return int", "fflush-stdin": "fflush(stdin) is UB", conio: "non-standard header",
  "unsafe-gets": "gets() is unsafe", "unsafe-system": "no system()", "unsafe-strcpy": "unbounded copy",
  "unsafe-strcat": "unbounded append", "unsafe-sprintf": "unbounded sprintf",
};
function shortWarning(d) {
  const m = /ignoring return value of '(\w+)'/.exec(d.message);
  if (m) return `${m[1]}() result ignored`;
  if (d.flag) return d.flag.replace(/^-W/, "");
  return fmt.clip(d.message, 26);
}
function shortFinding(f) {
  if (/overflow/.test(f.kind) && /integer/.test(f.kind)) return "overflows here";
  if (/leak/.test(f.kind)) return "never freed";
  if (/buffer-overflow/.test(f.kind)) return "out of bounds";
  if (/use-after-free/.test(f.kind)) return "used after free";
  if (/division/.test(f.kind)) return "divides by zero";
  return fmt.clip(f.kind, 24);
}

function collectNotes(report) {
  const notes = new Map();
  const add = (line, kind, text, title) => {
    line = Number(line);
    if (!line) return;
    if (!notes.has(line)) notes.set(line, []);
    const list = notes.get(line);
    if (text && list.some((n) => n.text === text)) return;
    list.push({ kind, text, title: title || text });
  };
  const flaggedMetrics = new Set(report.flags.filter((f) => /^suspected_(plagiarism|online)/.test(f.type)).map((f) => f.metric));
  for (const m of report.metrics) {
    const ev = m.evidence || {};
    if (m.analyzer === "compiler_warnings") {
      (ev.errors || []).forEach((d) => add(d.line, "err", "does not compile", d.message));
      (ev.warnings || []).forEach((d) => add(d.line, "warn", shortWarning(d), d.message));
    } else if (m.analyzer === "static_analysis") {
      (ev.issues || []).forEach((i) => add(i.line, "warn", SHORT_ISSUE[i.id] || fmt.clip(i.message, 26), i.message));
      (ev.functions || []).forEach((f) => {
        if (f.header_comment === false) add(f.line, "warn", "no header comment", `${f.name}() has no comment above it`);
        if (ev.limits && f.cyclomatic > ev.limits.max_cyclomatic) add(f.line, "warn", `too complex (${f.cyclomatic})`, `cyclomatic complexity ${f.cyclomatic}, limit ${ev.limits.max_cyclomatic}`);
        if (ev.limits && f.nloc > ev.limits.max_function_length) add(f.line, "warn", `${f.nloc} lines long`, `limit ${ev.limits.max_function_length}`);
      });
    } else if (m.analyzer === "sanitizer_run") {
      (ev.findings || []).forEach((f) => {
        const at = /:(\d+)/.exec(f.location || "");
        if (at) add(at[1], "err", shortFinding(f), `${f.kind}: ${f.message}`);
      });
    } else if (m.analyzer === "complexity_estimator") {
      const maxN = (ev.measurements || []).slice(-1)[0];
      (ev.hot_lines || []).filter((h) => h.count >= 1000).forEach((h) =>
        add(h.line, "hot", `runs ${fmt.compact(h.count)} times`,
          `executed ${fmt.full(h.count)} times at n = ${maxN ? fmt.full(maxN.n) : "?"} (${fmt.pct(h.share)} of all executed lines)`));
    } else if (m.analyzer === "similarity_flag" && flaggedMetrics.has(m.name)) {
      const top = (ev.top_matches || [])[0];
      if (top) {
        top.your_lines.forEach(([a, b], idx) => {
          for (let l = a; l <= b; l++) add(l, "copy", l === a && idx === 0 ? `same as ${top.file}` : "", `matches ${top.file}`);
        });
      }
    }
  }
  return notes;
}

/* The grade: a plain ring that fills to the grade while the number counts up (see countUp). */
const GAUGE_R = 52;
const GAUGE_C = 2 * Math.PI * GAUGE_R;
const gaugeTone = (fraction) => (fraction >= 0.75 ? "high" : fraction >= 0.5 ? "mid" : "low");

function ringSvg() {
  const full = GAUGE_C.toFixed(2);
  return svg("svg", { class: "grade-ring", viewBox: "0 0 120 120", "aria-hidden": "true" },
    svg("circle", { class: "gauge-track", cx: 60, cy: 60, r: GAUGE_R }),
    svg("circle", { class: "gauge-arc", cx: 60, cy: 60, r: GAUGE_R, "stroke-dasharray": full, "stroke-dashoffset": full }));
}
function drawRing(root, fraction) {
  const arc = root.querySelector(".gauge-arc");
  if (!arc) return;
  const target = GAUGE_C * (1 - Math.max(0, Math.min(1, fraction)));
  if (!reduceMotion) {
    arc.getBoundingClientRect();  // start the transition from the empty ring
    arc.style.transition = "stroke-dashoffset 1500ms cubic-bezier(.33,1,.68,1) 250ms";
  }
  arc.style.strokeDashoffset = target.toFixed(2);
}

/* The mark counts up from 0 to the grade while the ring fills. */
function countUp(root, grade) {
  const out = root.querySelector(".grade-value");
  const target = Number(grade);
  if (!out || reduceMotion || !Number.isFinite(target)) return;
  const final = out.textContent;
  const decimals = (final.split(".")[1] || "").length;
  const start = performance.now(), duration = 1500, delay = 250;
  out.textContent = (0).toFixed(decimals);
  const tick = (now) => {
    const t = Math.min(1, Math.max(0, (now - start - delay) / duration));
    const eased = 1 - Math.pow(1 - t, 3);
    out.textContent = (target * eased).toFixed(decimals);
    if (t < 1) requestAnimationFrame(tick);
    else out.textContent = final;
  };
  requestAnimationFrame(tick);
}

function firstPassBadges(fp) {
  const out = [fp.compiled ? badge("good", "Compiles") : badge("bad", "Does not compile")];
  if (fp.compiled) {
    if (fp.suspected_infinite_loop) out.push(badge("bad", "Sample run never finished"));
    else if (fp.smoke_verdict === "OK") out.push(badge("good", "Sample run finished"));
    else if (fp.smoke_verdict) out.push(badge("warn", `Sample run: ${VERDICT[fp.smoke_verdict] || fp.smoke_verdict}`));
    if (fp.sample_output_matches === true) out.push(badge("good", "Sample output correct"));
    if (fp.sample_output_matches === false) out.push(badge("bad", "Sample output wrong"));
  }
  return el("div", { class: "badges" }, out);
}

function scriptSheet(report, source, notes) {
  const kinds = new Set([...notes.values()].flat().map((n) => n.kind));
  const key = el("div", { class: "note-key" },
    kinds.has("hot") ? el("span", {}, el("i", { class: "key-hot" }), "most executed line") : null,
    kinds.has("err") ? el("span", {}, svg("svg", { class: "key-err", viewBox: "0 0 26 8", "aria-hidden": "true" },
      svg("path", { d: "M1 5Q4 1 7 5T13 5T19 5T25 5" })), "error found on this line") : null,
    kinds.has("copy") ? el("span", {}, el("i", { class: "key-copy" }), "matches another file") : null);
  let body;
  if (source == null) {
    body = el("p", { class: "no-source" }, "The source file is no longer where it was graded, so the margin notes cannot be shown.");
  } else {
    const lines = source.replace(/\n$/, "").split("\n");
    body = el("ol", { class: "script" }, lines.map((text, i) => {
      const n = i + 1;
      const ns = notes.get(n) || [];
      const cls = ["line", ...new Set(ns.map((x) => x.kind))].join(" ");
      return el("li", { class: cls, id: `L${n}` },
        el("span", { class: "ln", "aria-hidden": "true" }, n),
        el("span", { class: "src" }, el("span", { class: "ink" }, text || " ")),
        el("span", { class: "notes" }, ns.filter((x) => x.text).map((x) => el("span", { class: "note", title: x.title }, x.text))));
    }));
  }
  return el("section", { class: "sheet script-sheet", "aria-label": "Marked program" },
    el("div", { class: "grade-hero" },
      el("div", { class: "grade-line", role: "img", "aria-label": `Grade: ${report.grade} out of ${report.max_grade}` },
        el("div", { class: `grade-mark tone-${gaugeTone(report.grade / (report.max_grade || 100))}` },
          ringSvg(),
          el("div", { class: "grade-center", "aria-hidden": "true" },
            el("span", { class: "grade-value" }, fmt.pts(report.grade)),
            el("span", { class: "grade-max" }, `out of ${report.max_grade}`)))),
      firstPassBadges(report.first_pass)),
    el("h2", { class: "script-title" }, "Marked program"),
    key, body);
}

function jumpTo(line) {
  const li = document.getElementById(`L${line}`);
  if (!li) return;
  li.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
  li.classList.remove("pulse");
  void li.offsetWidth;
  li.classList.add("pulse");
}
const lineLink = (n) => el("button", { type: "button", class: "linelink", onclick: () => jumpTo(n) }, `line ${n}`);

/* ---------------------------------------------------------------- the marking sheet */
function markingSheet(report) {
  const sheet = el("section", { class: "sheet", "aria-label": "Marking sheet" }, scoreTable(report));
  if (report.flags.length) sheet.append(flagsBlock(report.flags));
  if (report.counterexamples.length) sheet.append(failuresBlock(report));
  const san = report.metrics.find((m) => m.analyzer === "sanitizer_run" && (m.evidence.findings || []).length);
  if (san) sheet.append(diagnosticsBlock(san));
  const cx = report.metrics.find((m) => m.analyzer === "complexity_estimator" && (m.evidence.measurements || []).length >= 2);
  if (cx) sheet.append(growthBlock(cx));
  if (report.suggestions.length) sheet.append(suggestionsBlock(report.suggestions));
  sheet.append(moreBlock(report));
  return sheet;
}

function scoreTable(report) {
  const maxW = Math.max(...report.metrics.map((m) => m.weight));
  const rows = report.metrics.map((m) => {
    const counted = m.score !== null;
    const possible = 100 * m.effective_weight;
    const earned = counted ? possible * m.score : 0;
    const notRun = m.status === "skipped_submission";
    const meter = el("div", { class: `meter${counted ? "" : " empty-track"}`, style: `width:${(m.weight / maxW) * 100}%` },
      counted ? el("span", { style: `width:${m.score * 100}%` }) : null);
    tipOn(meter, () => [el("strong", {}, fmt.title(m.name)), el("br"),
      counted ? `${fmt.pts(earned)} of ${fmt.pts(possible)} points (score ${m.score.toFixed(2)})` : "not counted on this machine"]);
    return el("tr", { class: !counted ? "not-counted" : notRun ? "not-run" : "" },
      el("th", { scope: "row" }, fmt.title(m.name), el("span", { class: "analyzer" }, m.analyzer)),
      el("td", { class: "bar-cell" }, meter),
      el("td", { class: "pts" }, counted ? `${fmt.pts(earned)} / ${fmt.pts(possible)}` : "not counted"),
      el("td", { class: "verdict" }, m.summary));
  });
  return el("div", {},
    el("h2", {}, "Score by metric"),
    el("p", { class: "lede muted small", style: "margin:4px 0 8px" },
      "Bar length is the metric's weight; the filled part is what this program earned."),
    el("table", { class: "score-table" },
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Metric"), el("th", { scope: "col" }, "Weight and score"),
        el("th", { scope: "col", style: "text-align:right;padding-right:14px" }, "Points"), el("th", { scope: "col" }, "Verdict"))),
      el("tbody", {}, rows)));
}

function flagsBlock(flags) {
  return el("div", { class: "block" },
    el("h2", {}, "Flags for the instructor"),
    el("ul", { class: "flag-list" }, flags.map((f) => el("li", {}, badge(flagKind(f), flagLabel(f)), el("span", { class: "msg" }, rich(f.message))))));
}

function ioBox(title, text, size, cls) {
  const shown = (text == null ? "" : text).replace(/\.\.\. \[\d+ more chars\]$/, "…");
  const body = shown === "" ? "(nothing)" : shown.replace(/\n$/, "") === "" ? "(a blank line)" : shown.replace(/\n$/, "");
  return el("div", { class: `io-box ${cls || ""}` },
    el("h4", {}, title),
    el("pre", {}, body),
    size && size > shown.length ? el("p", { class: "cut" }, `first part of ${fmt.full(size)} characters`) : null);
}

function failuresBlock(report) {
  return el("div", { class: "block" },
    el("h2", {}, "Where it fails"),
    el("p", { class: "lede" }, "Each case is an input this program gets wrong, the answer expected, and why."),
    report.counterexamples.map((ce) => {
      const title = ce.label && ce.label.startsWith("Test") ? `Hidden ${ce.label.toLowerCase()}` : `Generated ${ce.source.replace(/^edge case: /, "").replace(/_/g, " ")} case`;
      const sizes = ce.sizes || {};
      return el("article", { class: "answer" },
        el("div", { class: "answer-head" },
          el("h3", {}, title),
          badge("bad", VERDICT[ce.verdict] || ce.verdict),
          ce.minimized ? badge("info", "shrunk to a minimal input") : null,
          el("span", { class: "origin" }, fmt.title(ce.metric))),
        el("div", { class: "io" },
          ioBox("Input", ce.input, sizes.input),
          ce.expected != null ? ioBox("Expected", ce.expected, sizes.expected) : null,
          ioBox("Your program", ce.got, sizes.got, "got")),
        el("p", { class: "why" }, rich(ce.why)),
        ce.hint ? el("p", { class: "fix" }, "How to fix it: ", rich(ce.hint)) : null,
        ce.original_input ? el("details", {}, el("summary", {}, "The larger input it was shrunk from"), el("pre", {}, ce.original_input)) : null,
        el("p", { class: "repro" }, "Reproduce with ", el("code", {}, `./prog < ${ce.files.input}`)));
    }));
}

function diagnosticsBlock(m) {
  const tool = { asan: "AddressSanitizer and UBSan", valgrind: "Valgrind", "ubsan-trap": "UBSan (trap mode)" }[m.evidence.backend] || "sanitizers";
  return el("div", { class: "block" },
    el("h2", {}, "Memory and undefined behaviour"),
    el("p", { class: "lede" }, `Found by ${tool} while running the tests.`),
    el("ul", { class: "diag-list" }, m.evidence.findings.map((f) => {
      const at = /:(\d+)/.exec(f.location || "");
      return el("li", {},
        el("span", { class: "kind" }, fmt.title(f.kind)), " ",
        at ? ["at ", lineLink(Number(at[1])), f.location.includes(" in ") ? ` in ${f.location.split(" in ")[1]}` : ""] : "(location unknown)",
        el("div", { class: "muted" }, `${f.message}. First seen on ${f.trigger}.`));
    })));
}

function growthBlock(m) {
  const ev = m.evidence;
  const series = [{ name: "This program", color: "var(--series-1)", points: ev.measurements.map((p) => ({ x: p.n, y: p.ops })) }];
  if (ev.reference_ops && ev.reference_ops.length) {
    series.push({ name: "Reference solution", color: "var(--series-2)", points: ev.reference_ops.map((p) => ({ x: p.n, y: p.ops })) });
  }
  const gcov = String(ev.method).startsWith("gcov");
  const unit = gcov ? "executed lines" : "microseconds";
  return el("div", { class: "block" },
    el("h2", {}, "How the work grows with n"),
    el("p", { class: "lede" },
      `Measured ${ev.estimated}; the problem requires ${ev.required}. ` +
      (gcov ? "Each point counts the source lines executed (gcov), so the curve is exact and repeats on every run."
        : "Measured with CPU time because gcov is not available, so small differences are noise.")),
    series.length > 1 ? el("div", { class: "legend" }, series.map((s) => el("span", {}, el("i", { style: `background:${s.color}` }), s.name))) : null,
    lineChart(series, unit),
    (ev.hot_lines || []).length && ev.hot_lines[0].count >= 1000
      ? el("ul", { class: "hot-list" }, ev.hot_lines.map((h) => el("li", {}, lineLink(h.line), ` runs ${fmt.full(h.count)} times (${fmt.pct(h.share)}): `, el("code", {}, h.code))))
      : null,
    el("details", { class: "more" }, el("summary", {}, "Show the measurements as a table"),
      el("table", { class: "table-view" },
        el("thead", {}, el("tr", {}, el("th", {}, "n"), series.map((s) => el("th", {}, `${s.name} (${unit})`)))),
        el("tbody", {}, series[0].points.map((p, i) => el("tr", {}, el("td", {}, fmt.full(p.x)),
          series.map((s) => el("td", {}, s.points[i] ? fmt.full(s.points[i].y) : "–"))))))));
}

/* A log-log line chart: one y axis, 2px lines, ringed markers, direct end labels,
   crosshair + tooltip on hover, arrow keys move the crosshair. */
function lineChart(series, unit) {
  const W = 600, H = 250, M = { l: 54, r: 132, t: 12, b: 34 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const xs = series[0].points.map((p) => p.x);
  const ys = series.flatMap((s) => s.points.map((p) => Math.max(1, p.y)));
  const lg = Math.log10;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = 10 ** Math.floor(lg(Math.min(...ys)));
  let y1 = 10 ** Math.ceil(lg(Math.max(...ys)));
  if (y1 <= y0) y1 = y0 * 10;
  const sx = (v) => (x1 === x0 ? M.l + pw / 2 : M.l + ((lg(v) - lg(x0)) / (lg(x1) - lg(x0))) * pw);
  const sy = (v) => M.t + ph - ((lg(Math.max(1, v)) - lg(y0)) / (lg(y1) - lg(y0))) * ph;

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0",
    "aria-label": `${series.map((s) => `${s.name}: ${s.points.map((p) => `${fmt.full(p.y)} at n=${fmt.full(p.x)}`).join(", ")}`).join(". ")}` });
  const decades = Math.round(lg(y1) - lg(y0));
  const step = Math.max(1, Math.ceil(decades / 5));
  for (let e = lg(y0); e <= lg(y1) + 1e-9; e += step) {
    const v = 10 ** e, y = sy(v);
    root.append(svg("line", { class: "gridline", x1: M.l, x2: W - M.r, y1: y, y2: y }),
      svg("text", { x: M.l - 8, y: y + 4, "text-anchor": "end" }, fmt.compact(Math.round(v))));
  }
  root.append(svg("line", { class: "axisline", x1: M.l, x2: W - M.r, y1: M.t + ph, y2: M.t + ph }));
  xs.forEach((x) => root.append(svg("text", { x: sx(x), y: H - 12, "text-anchor": "middle" }, fmt.compact(x))));
  root.append(svg("text", { x: W - M.r + 34, y: H - 12, "text-anchor": "start" }, "n"));

  series.forEach((s) => {
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");
    root.append(svg("path", { class: "series-line", d, style: `stroke:${s.color}` }));
    s.points.forEach((p) => root.append(svg("circle", { class: "marker", cx: sx(p.x), cy: sy(p.y), r: 4, style: `fill:${s.color}` })));
  });
  // Direct end labels, only when they do not collide (the legend always carries identity).
  const ends = series.map((s) => ({ s, p: s.points[s.points.length - 1] })).map((e) => ({ ...e, y: sy(e.p.y) }));
  const collide = ends.length > 1 && Math.abs(ends[0].y - ends[1].y) < 30;
  if (!collide) {
    ends.forEach(({ s, p, y }) => root.append(
      svg("text", { class: "end-label", x: sx(p.x) + 10, y: y - 2 }, s.name),
      svg("text", { class: "end-label", x: sx(p.x) + 10, y: y + 13 }, fmt.compact(p.y))));
  }

  const cross = svg("line", { class: "crosshair", y1: M.t, y2: M.t + ph, visibility: "hidden" });
  const halo = series.map((s) => svg("circle", { r: 6, style: `fill:${s.color}`, class: "marker", visibility: "hidden" }));
  root.append(cross, ...halo);
  const hit = svg("rect", { x: M.l - 20, y: 0, width: pw + 40, height: H, fill: "transparent" });
  root.append(hit);

  let current = -1;
  const show = (i, clientX, clientY) => {
    current = i;
    const x = sx(xs[i]);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    series.forEach((s, k) => {
      const p = s.points[i];
      if (!p) { halo[k].setAttribute("visibility", "hidden"); return; }
      halo[k].setAttribute("cx", x); halo[k].setAttribute("cy", sy(p.y)); halo[k].setAttribute("visibility", "visible");
    });
    if (clientX === undefined) {
      const box = root.getBoundingClientRect();
      clientX = box.left + (x / W) * box.width;
      clientY = box.top + box.height * 0.25;
    }
    showTip([el("strong", {}, `n = ${fmt.full(xs[i])}`),
      ...series.filter((s) => s.points[i]).map((s) => el("div", { class: "tt-row" }, el("i", { style: `background:${s.color}` }),
        `${s.name}: ${fmt.full(s.points[i].y)} ${unit}`))], clientX, clientY);
  };
  const hide = () => {
    current = -1;
    cross.setAttribute("visibility", "hidden");
    halo.forEach((h) => h.setAttribute("visibility", "hidden"));
    hideTip();
  };
  hit.addEventListener("pointermove", (e) => {
    const box = root.getBoundingClientRect();
    const vx = ((e.clientX - box.left) / box.width) * W;
    let best = 0;
    xs.forEach((x, i) => { if (Math.abs(sx(x) - vx) < Math.abs(sx(xs[best]) - vx)) best = i; });
    show(best, e.clientX, e.clientY);
  });
  hit.addEventListener("pointerleave", hide);
  root.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const next = current < 0 ? 0 : Math.min(xs.length - 1, Math.max(0, current + (e.key === "ArrowRight" ? 1 : -1)));
      show(next);
    } else if (e.key === "Escape") hide();
  });
  root.addEventListener("blur", hide);
  return el("div", { class: "chart" }, root);
}

function suggestionsBlock(list) {
  return el("div", { class: "block" },
    el("h2", {}, "How to raise the grade"),
    el("p", { class: "lede" }, "Ordered by the points each change can recover."),
    el("ol", { class: "suggestions" }, list.map((s) => el("li", {}, rich(s.suggestion), " ",
      el("span", { class: "gain" }, `(${fmt.title(s.metric)}, up to ${fmt.pts(s.points_at_stake)} points)`)))));
}

function moreBlock(report) {
  return el("div", { class: "block" },
    el("details", { class: "more" }, el("summary", {}, "Feedback from every analyzer"),
      el("dl", { class: "feedback-list" }, report.metrics.map((m) => [el("dt", {}, fmt.title(m.name)), el("dd", {}, rich(m.feedback))]))));
}

/* ================================================================ Class results */
const HEAT = [[0.95, "s600"], [0.75, "s500"], [0.5, "s300"], [0.25, "s200"], [-1, "s100"]];
const heatClass = (score) => HEAT.find(([min]) => score >= min)[1];

async function renderResults(groupName) {
  const { groups } = await api("/api/reports");
  main.append(el("div", { class: "page-head" }, el("h1", {}, "Class results"),
    groups.length > 1 ? choice("Folder", groups.map((g) => ({ value: g.name, label: g.name })), (groups.find((g) => g.name === groupName) || groups[0]).name,
      (name) => { location.hash = `#/results/${encodeURIComponent(name)}`; }) : null));
  if (!groups.length) {
    main.append(el("section", { class: "sheet empty" },
      el("h2", {}, "Nothing graded yet"),
      el("p", {}, "Grade a program on the Grade page, or grade a whole folder from a terminal:"),
      el("p", {}, el("code", {}, "python -m autograder grade <folder with .c files> --rules <rules.json>"))));
    return;
  }
  const g = groups.find((x) => x.name === groupName) || groups[0];
  const metricNames = g.reports[0].metrics.map((m) => m.name);
  const weights = Object.fromEntries(g.reports[0].metrics.map((m) => [m.name, m.weight]));
  const grades = g.reports.map((r) => r.grade).sort((a, b) => a - b);
  const median = grades.length % 2 ? grades[(grades.length - 1) / 2] : (grades[grades.length / 2 - 1] + grades[grades.length / 2]) / 2;
  const flagged = g.reports.filter((r) => r.flags.some((f) => f.severity === "high")).length;

  const rows = g.reports.map((r) => {
    const href = `#/report/file/${encodeURIComponent(r.file)}`;
    const byName = Object.fromEntries(r.metrics.map((m) => [m.name, m]));
    const tr = el("tr", { onclick: (e) => { if (!e.target.closest("a")) location.hash = href; } },
      el("th", { scope: "row" }, el("a", { href }, r.submission)),
      el("td", { class: "grade-cell" }, el("span", { class: "gnum" }, fmt.pts(r.grade)),
        el("span", { class: "meter", "aria-hidden": "true" }, el("span", { style: `width:${r.grade}%` }))),
      metricNames.map((n) => {
        const m = byName[n];
        if (!m || m.score === null) return el("td", { class: "heat none" }, "–");
        const cell = el("td", { class: `heat ${heatClass(m.score)}` }, fmt.pts(m.contribution));
        tipOn(cell, () => [el("strong", {}, `${r.submission}: ${fmt.title(n)}`), el("br"),
          `${fmt.pts(m.contribution)} of ${fmt.pts(100 * m.effective_weight)} points`, el("br"), fmt.clip(m.summary, 90)]);
        return cell;
      }),
      el("td", { class: "flags-cell" }, el("div", { class: "badges" },
        [...new Map(r.flags.filter((f) => f.severity !== "info").map((f) => [f.type, f])).values()]
          .map((f) => badge(flagKind(f), flagLabel(f))))));
    return tr;
  });

  main.append(
    el("p", { class: "results-summary" },
      `${g.reports.length} program${g.reports.length === 1 ? "" : "s"} graded, median grade ${fmt.pts(median)}` +
      (flagged ? `, ${flagged} flagged for review.` : ".")),
    el("section", { class: "sheet" },
      el("div", { class: "results-wrap" },
        el("table", { class: "results" },
          el("caption", { class: "visually-hidden" }, `Grades for ${g.name}; each metric cell shows the points earned`),
          el("thead", {}, el("tr", {},
            el("th", { scope: "col" }, "Program"), el("th", { scope: "col" }, "Grade"),
            metricNames.map((n) => el("th", { scope: "col" }, fmt.title(n), el("span", { class: "w" }, `of ${fmt.pts(weights[n] * 100)}`))),
            el("th", { scope: "col" }, "Flags"))),
          el("tbody", {}, rows))),
      el("div", { class: "heat-key" }, "Share of a metric's points earned:",
        el("span", { class: "sw", "aria-hidden": "true" }, ["--seq-100", "--seq-200", "--seq-300", "--seq-500", "--seq-600"].map((v) => el("i", { style: `background:var(${v})` }))),
        "under 25% to 95% or more. Select a program to open its report.")));
}

/* ================================================================ start */
window.addEventListener("hashchange", route);
loadStatus();
route();

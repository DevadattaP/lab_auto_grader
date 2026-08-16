"use strict";

/* ---------------------------------------------------------------------- */
/* small utilities                                                        */
/* ---------------------------------------------------------------------- */

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k.startsWith("data-")) node.setAttribute(k, v);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(url, options) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const isJson = (resp.headers.get("content-type") || "").includes("application/json");
  const body = isJson ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const message = isJson && body && body.error ? body.error : `${resp.status} ${resp.statusText}`;
    throw new Error(message);
  }
  return body;
}

// input.value is always a string, even for type="number" -- convert here
// rather than sending numeric fields to the server as JSON strings.
function numOrNull(inputEl) {
  const v = inputEl.value;
  return v === "" ? null : parseFloat(v);
}

let META = null;

/* ---------------------------------------------------------------------- */
/* tree                                                                    */
/* ---------------------------------------------------------------------- */

async function loadTree() {
  const data = await api("/api/tree");
  renderTree(data);
}

function renderTree(data) {
  const root = document.getElementById("tree-root");
  root.innerHTML = "";

  const header = el(
    "div",
    { class: "row" },
    el("strong", {}, "Auto-grader"),
    el(
      "button",
      { class: "link small-btn", type: "button", onclick: onAddLab },
      "+ Add Lab"
    )
  );
  root.append(header);

  const labUl = el("ul", { class: "tree root" });
  for (const lab of data.labs) {
    labUl.append(renderLab(lab));
  }
  root.append(labUl);
}

function renderLab(lab) {
  const details = el("details", { name: "labs", "data-lab": lab.id });
  const summary = el(
    "summary",
    {},
    el("span", { class: "arrow" }),
    lab.id,
    el(
      "button",
      {
        class: "link small-btn lab-delete-btn",
        type: "button",
        onclick: (ev) => {
          ev.preventDefault();
          onDeleteLab(lab.id);
        },
      },
      "Delete"
    )
  );
  details.append(summary);

  const sectionsName = `${lab.id}__sections`;
  const ul = el("ul", { class: "tree" });
  ul.append(renderDashboardSection(lab, sectionsName));
  ul.append(renderQuestionsSection(lab, sectionsName));
  ul.append(renderSubmissionsSection(lab, sectionsName));
  ul.append(renderResultSection(lab, sectionsName));
  details.append(ul);

  return el("li", {}, details);
}

/* ---------------------------------------------------------------------- */
/* dashboard section (question-wise / student-wise cross-reference)       */
/* ---------------------------------------------------------------------- */

function renderDashboardSection(lab, sectionsName) {
  const dash = lab.dashboard || { by_question: {}, by_student: {} };
  const qids = Object.keys(dash.by_question);
  const students = Object.keys(dash.by_student);

  if (qids.length === 0 && students.length === 0) {
    return el("li", {}, el("span", { class: "empty" }, "No dashboard data"));
  }

  const details = el("details", { name: sectionsName });
  details.append(el("summary", {}, el("span", { class: "arrow" }), "dashboard"));

  const modeName = `${lab.id}__dashboard_mode`;
  const ul = el("ul", { class: "tree" });
  ul.append(renderDashboardByQuestion(lab.id, dash, modeName));
  ul.append(renderDashboardByStudent(lab.id, dash, modeName));
  details.append(ul);
  return el("li", {}, details);
}

function renderDashboardByQuestion(labId, dash, modeName) {
  const details = el("details", { name: modeName });
  details.append(el("summary", {}, el("span", { class: "arrow" }), "by question"));
  const ul = el("ul", { class: "tree" });
  const qids = Object.keys(dash.by_question);
  if (qids.length === 0) {
    ul.append(el("li", {}, el("span", { class: "empty" }, "No questions")));
  }
  const qOpenName = `${labId}__dashboard_by_question_open`;
  for (const qid of qids) {
    const students = dash.by_question[qid];
    const qDetails = el("details", { name: qOpenName });
    qDetails.append(el("summary", {}, el("span", { class: "arrow" }), `${qid} (${students.length})`));
    const sUl = el("ul", { class: "tree" });
    if (students.length === 0) {
      sUl.append(el("li", {}, el("span", { class: "empty" }, "No submissions")));
    } else {
      for (const student of students) {
        sUl.append(
          el(
            "li",
            {},
            el(
              "button",
              { class: "link row-label leaf", type: "button", onclick: () => openDashboardModal(labId, qid, student) },
              student
            )
          )
        );
      }
    }
    qDetails.append(sUl);
    ul.append(el("li", {}, qDetails));
  }
  details.append(ul);
  return el("li", {}, details);
}

function renderDashboardByStudent(labId, dash, modeName) {
  const details = el("details", { name: modeName });
  details.append(el("summary", {}, el("span", { class: "arrow" }), "by student"));
  const ul = el("ul", { class: "tree" });
  const students = Object.keys(dash.by_student);
  if (students.length === 0) {
    ul.append(el("li", {}, el("span", { class: "empty" }, "No students")));
  }
  const sOpenName = `${labId}__dashboard_by_student_open`;
  for (const student of students) {
    const qids = dash.by_student[student];
    const sDetails = el("details", { name: sOpenName });
    sDetails.append(el("summary", {}, el("span", { class: "arrow" }), `${student} (${qids.length})`));
    const qUl = el("ul", { class: "tree" });
    if (qids.length === 0) {
      qUl.append(el("li", {}, el("span", { class: "empty" }, "No submissions")));
    } else {
      for (const qid of qids) {
        qUl.append(
          el(
            "li",
            {},
            el(
              "button",
              { class: "link row-label leaf", type: "button", onclick: () => openDashboardModal(labId, qid, student) },
              qid
            )
          )
        );
      }
    }
    sDetails.append(qUl);
    ul.append(el("li", {}, sDetails));
  }
  details.append(ul);
  return el("li", {}, details);
}

async function openDashboardModal(labId, qid, student) {
  const dialog = document.getElementById("dashboard-modal");
  const title = document.getElementById("dashboard-modal-title");
  const reportBox = document.getElementById("dashboard-report");
  const studentBox = document.getElementById("dashboard-student-code");
  const goldBox = document.getElementById("dashboard-gold-code");

  title.textContent = `${labId} / ${qid} / ${student}`;
  reportBox.innerHTML = "Loading...";
  studentBox.innerHTML = "";
  goldBox.innerHTML = "";
  dialog.showModal();

  try {
    const data = await api(
      `/api/labs/${encodeURIComponent(labId)}/dashboard/${encodeURIComponent(qid)}/${encodeURIComponent(student)}`
    );
    title.textContent = `${labId} / ${qid}: ${data.title} / ${student}`;
    reportBox.innerHTML =
      data.report_html || `<span class="empty">No report section found for this question/student.</span>`;
    studentBox.innerHTML =
      data.student_code_html || `<span class="empty">No matching submission file found.</span>`;
    goldBox.innerHTML = data.gold_code_html || `<span class="empty">No gold.c found for this question.</span>`;
  } catch (e) {
    reportBox.innerHTML = "";
    reportBox.append(el("div", { class: "error-msg" }, `Could not load dashboard data: ${e.message}`));
  }
}

function renderQuestionsSection(lab, sectionsName) {
  const addBtn = el(
    "button",
    {
      class: "link small-btn",
      type: "button",
      onclick: (ev) => {
        ev.preventDefault();
        openQuestionModal(lab.id, null);
      },
    },
    "+ Add Question"
  );

  if (lab.questions.length === 0) {
    return el("li", {}, el("span", { class: "empty" }, "No questions"), addBtn);
  }

  const details = el("details", { name: sectionsName });
  details.append(el("summary", {}, el("span", { class: "arrow" }), "questions", addBtn));
  const ul = el("ul", { class: "tree" });
  for (const qid of lab.questions) {
    ul.append(
      el(
        "li",
        {},
        el(
          "button",
          {
            class: "link row-label leaf",
            type: "button",
            onclick: () => openQuestionModal(lab.id, qid),
          },
          qid
        )
      )
    );
  }
  details.append(ul);
  return el("li", {}, details);
}

function renderSubmissionsSection(lab, sectionsName) {
  const students = Object.keys(lab.submissions);
  if (students.length === 0) {
    return el("li", {}, el("span", { class: "empty" }, "No submissions"));
  }

  const details = el("details", { name: sectionsName });
  details.append(el("summary", {}, el("span", { class: "arrow" }), "submissions"));
  const ul = el("ul", { class: "tree" });
  for (const student of students) {
    const files = lab.submissions[student];
    const studentDetails = el("details", {});
    studentDetails.append(el("summary", {}, el("span", { class: "arrow" }), student));
    const fileUl = el("ul", { class: "tree" });
    if (files.length === 0) {
      fileUl.append(el("li", {}, el("span", { class: "empty" }, "No files")));
    } else {
      for (const file of files) {
        fileUl.append(
          el(
            "li",
            {},
            el(
              "button",
              {
                class: "link row-label leaf",
                type: "button",
                onclick: () => openCodeModal(lab.id, student, file),
              },
              file
            )
          )
        );
      }
    }
    studentDetails.append(fileUl);
    ul.append(el("li", {}, studentDetails));
  }
  details.append(ul);
  return el("li", {}, details);
}

function renderResultSection(lab, sectionsName) {
  if (!lab.result) {
    return el("li", {}, el("span", { class: "empty" }, "No result"));
  }

  const details = el("details", { name: sectionsName });
  details.append(
    el("summary", {}, el("span", { class: "arrow" }), `result (${lab.result.run_id})`)
  );
  const ul = el("ul", { class: "tree" });
  for (const file of lab.result.files) {
    ul.append(
      el(
        "li",
        {},
        el(
          "button",
          {
            class: "link row-label leaf",
            type: "button",
            onclick: () => openResultModal(lab.id, file),
          },
          file
        )
      )
    );
  }
  details.append(ul);
  return el("li", {}, details);
}

/* ---------------------------------------------------------------------- */
/* lab add / delete                                                       */
/* ---------------------------------------------------------------------- */

async function onAddLab() {
  const id = prompt("New lab id (e.g. lab_02):");
  if (!id) return;
  try {
    await api("/api/labs", { method: "POST", body: JSON.stringify({ id: id.trim() }) });
    await loadTree();
  } catch (e) {
    alert(`Could not create lab: ${e.message}`);
  }
}

async function onDeleteLab(labId) {
  if (!confirm(`Delete lab "${labId}"? This removes its questions, submissions, and results. This cannot be undone.`)) {
    return;
  }
  try {
    await api(`/api/labs/${encodeURIComponent(labId)}`, { method: "DELETE" });
    await loadTree();
  } catch (e) {
    alert(`Could not delete lab: ${e.message}`);
  }
}

/* ---------------------------------------------------------------------- */
/* code modal                                                              */
/* ---------------------------------------------------------------------- */

async function openCodeModal(labId, student, filename) {
  const dialog = document.getElementById("code-modal");
  const title = document.getElementById("code-modal-title");
  const body = document.getElementById("code-modal-body");
  title.textContent = `${labId} / ${student} / ${filename}`;
  body.innerHTML = "Loading...";
  dialog.showModal();
  try {
    const data = await api(
      `/api/labs/${encodeURIComponent(labId)}/submissions/${encodeURIComponent(student)}/${encodeURIComponent(filename)}`
    );
    body.innerHTML = data.html;
  } catch (e) {
    body.innerHTML = "";
    body.append(el("div", { class: "error-msg" }, `Could not load file: ${e.message}`));
  }
}

/* ---------------------------------------------------------------------- */
/* result modal                                                            */
/* ---------------------------------------------------------------------- */

async function openResultModal(labId, filename) {
  const dialog = document.getElementById("result-modal");
  const title = document.getElementById("result-modal-title");
  const body = document.getElementById("result-modal-body");
  title.textContent = `${labId} / ${filename}`;
  body.innerHTML = "Loading...";
  dialog.showModal();
  try {
    const data = await api(`/api/labs/${encodeURIComponent(labId)}/result/${encodeURIComponent(filename)}`);
    if (data.kind === "csv") {
      body.innerHTML = "";
      body.append(renderCsvTable(data.rows));
    } else {
      body.innerHTML = data.html;
    }
  } catch (e) {
    body.innerHTML = "";
    body.append(el("div", { class: "error-msg" }, `Could not load file: ${e.message}`));
  }
}

function renderCsvTable(rows) {
  const table = el("table", { class: "csv-table" });
  rows.forEach((row, i) => {
    const tr = el("tr", {});
    const cellTag = i === 0 ? "th" : "td";
    for (const cell of row) {
      tr.append(el(cellTag, {}, cell));
    }
    table.append(tr);
  });
  return table;
}

/* ---------------------------------------------------------------------- */
/* question editor modal                                                   */
/* ---------------------------------------------------------------------- */

function defaultQuestionPayload() {
  const d = META.defaults;
  return {
    id: "",
    title: "",
    description: "",
    filename_patterns: [],
    marks_open: 0,
    marks_hidden: 0,
    compile_standard: d.compile_standard,
    compile_flags_common: [...d.compile_flags_common],
    compile_flags_extra: "",
    limits: { ...d.limits },
    matcher_type: d.matcher_type,
    matcher_eps: null,
    matcher_ignore_case: false,
    matcher_ignore_whitespace: false,
    matcher_ignore_punctuation: false,
    matcher_allow_extra_output: false,
    matcher_symbol_groups: "",
    adjust_char_input: false,
    adjust_scanf_address: false,
    code_checks_enabled: false,
    code_checks_mode: d.code_checks_mode,
    code_checks_require_builtin: [],
    code_checks_require_extra: "",
    code_checks_require_match: d.code_checks_require_match,
    code_checks_forbid_builtin: [],
    code_checks_forbid_extra: "",
    code_checks_marks: null,
    code_checks_penalty: null,
    code_checks_line_gate_text: "",
    tests_open: [],
    tests_hidden: [],
    gold_c: "",
  };
}

async function openQuestionModal(labId, qid) {
  const dialog = document.getElementById("question-modal");
  const title = document.getElementById("question-modal-title");
  const body = document.getElementById("question-modal-body");
  const isNew = qid === null;
  title.textContent = isNew ? `${labId} / (new question)` : `${labId} / ${qid}`;
  body.innerHTML = "Loading...";
  dialog.showModal();

  let payload;
  try {
    payload = isNew
      ? defaultQuestionPayload()
      : await api(`/api/labs/${encodeURIComponent(labId)}/questions/${encodeURIComponent(qid)}`);
  } catch (e) {
    body.innerHTML = "";
    body.append(el("div", { class: "error-msg" }, `Could not load question: ${e.message}`));
    return;
  }

  body.innerHTML = "";
  body.append(buildQuestionForm(labId, qid, payload, isNew));
}

/* --- small form-building helpers --- */

function field(labelText, inputEl) {
  return el("div", { class: "field" }, el("label", {}, labelText), inputEl);
}

function textInput(value, attrs) {
  return el("input", { type: "text", value: value || "", ...attrs });
}

function numberInput(value, attrs) {
  return el("input", { type: "number", value: value === null || value === undefined ? "" : value, ...attrs });
}

function textarea(value, attrs) {
  return el("textarea", { rows: 3, ...attrs }, value || "");
}

function radioRow(name, options, selected, onChange) {
  const row = el("div", { class: "radio-row" });
  for (const opt of options) {
    const input = el("input", {
      type: "radio",
      name,
      value: opt,
      checked: opt === selected,
      onchange: onChange,
    });
    row.append(el("label", {}, input, opt));
  }
  return row;
}

function checkboxGrid(groupAttr, options, selectedList, onChange) {
  const grid = el("div", { class: "checkbox-grid" });
  const selected = new Set(selectedList || []);
  for (const opt of options) {
    const input = el("input", {
      type: "checkbox",
      "data-group": groupAttr,
      value: opt,
      checked: selected.has(opt),
      onchange: onChange,
    });
    grid.append(el("label", {}, input, opt));
  }
  return grid;
}

/* --- test case rows --- */

function buildTestCase(group, test) {
  const nameInput = textInput(test.name, { required: true, "data-field": "name" });
  const weightInput = numberInput(test.weight, { step: "any", required: true, "data-field": "weight" });
  const inArea = textarea(test.in, { class: "code", "data-field": "in" });
  const outArea = textarea(test.out, { class: "code", "data-field": "out" });
  const partialInput = el("input", {
    type: "checkbox",
    checked: !!test.partial,
    "data-field": "partial",
  });
  const ignoreTypoInput = el("input", {
    type: "checkbox",
    checked: !!test.ignore_typo,
    "data-field": "ignore_typo",
  });

  const summaryLabel = el("span", {}, test.name || "(unnamed)", " — weight ", String(test.weight));
  nameInput.addEventListener("input", () => {
    summaryLabel.textContent = `${nameInput.value || "(unnamed)"} — weight ${weightInput.value || ""}`;
  });
  weightInput.addEventListener("input", () => {
    summaryLabel.textContent = `${nameInput.value || "(unnamed)"} — weight ${weightInput.value || ""}`;
  });

  const details = el(
    "details",
    {
      class: "test-case",
      "data-group": group,
      name: `tc-${group}`,
      open: !test.name ? true : undefined,
    },
    el(
      "summary",
      {},
      el("span", { class: "arrow" }),
      summaryLabel
    ),
    field("name", nameInput),
    field("weight", weightInput),
    field("in (stdin)", inArea),
    field("out (expected stdout)", outArea),
    field(
      "partial credit -- award weight/N marks per matching line of expected " +
        "output instead of all-or-nothing (e.g. a 2-line test where each line " +
        "checks something independent: 1 correct line = half marks)",
      partialInput
    ),
    field(
      "ignore typo -- tolerate a 1-character spelling slip in a word of the " +
        "expected output (e.g. \"aperator\" for \"operator\"), tried only if " +
        "the test would otherwise fail; never applies to numbers",
      ignoreTypoInput
    ),
    el(
      "button",
      {
        type: "button",
        class: "link small-btn",
        onclick: () => details.remove(),
      },
      "Remove this test"
    )
  );
  return details;
}

function gatherTestCases(container, group) {
  const tests = [];
  container.querySelectorAll(`.test-case[data-group="${group}"]`).forEach((tc) => {
    tests.push({
      name: tc.querySelector('[data-field="name"]').value.trim(),
      weight: parseFloat(tc.querySelector('[data-field="weight"]').value || "0"),
      in: tc.querySelector('[data-field="in"]').value,
      out: tc.querySelector('[data-field="out"]').value,
      partial: tc.querySelector('[data-field="partial"]').checked,
      ignore_typo: tc.querySelector('[data-field="ignore_typo"]').checked,
    });
  });
  return tests;
}

/* --- the form itself --- */

function buildQuestionForm(labId, qid, payload, isNew) {
  const form = el("form", {});
  const errorBox = el("div", {});

  // Basic info
  const idInput = textInput(payload.id, { required: true, disabled: !isNew });
  const titleInput = textInput(payload.title);
  const descArea = textarea(payload.description, { rows: 3 });
  const patternsArea = textarea((payload.filename_patterns || []).join("\n"), {
    rows: 3,
    placeholder: "one filename per line, e.g. q1.c",
  });

  const basicInfo = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Basic info"),
    field("id (must match the folder name; fixed once created)", idInput),
    field("title", titleInput),
    field("description", descArea),
    field("filename_patterns (one per line)", patternsArea)
  );

  // Marks
  const marksOpenInput = numberInput(payload.marks_open, { step: "any", required: true });
  const marksHiddenInput = numberInput(payload.marks_hidden, { step: "any", required: true });
  const marksSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Marks"),
    el(
      "div",
      { class: "inline-fields" },
      field("open (sum of open test weights must equal this)", marksOpenInput),
      field("hidden (sum of hidden test weights must equal this)", marksHiddenInput)
    )
  );

  // Compile
  const standardRow = radioRow("compile_standard", META.compile_standards, payload.compile_standard);
  const flagsGrid = checkboxGrid("compile_flags", META.common_compile_flags, payload.compile_flags_common);
  const flagsExtra = textInput(payload.compile_flags_extra, { placeholder: "additional flags, space-separated" });
  const compileSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Compile"),
    field("standard", standardRow),
    field("flags", flagsGrid),
    field("additional flags", flagsExtra)
  );

  // Limits
  const limits = payload.limits || {};
  const timeInput = numberInput(limits.time_seconds, { step: "0.1", min: "0" });
  const wallInput = numberInput(limits.wall_seconds, { step: "0.1", min: "0" });
  const memInput = numberInput(limits.memory_mb, { step: "1", min: "1" });
  const outputBytesInput = numberInput(limits.output_bytes, { step: "1", min: "1" });
  const maxProcInput = numberInput(limits.max_processes, { step: "1", min: "1" });
  const limitsSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Limits"),
    el(
      "div",
      { class: "inline-fields" },
      field("time_seconds (CPU)", timeInput),
      field("wall_seconds", wallInput),
      field("memory_mb", memInput),
      field("output_bytes", outputBytesInput),
      field("max_processes", maxProcInput)
    )
  );

  // Matcher
  const epsField = field("eps (float_tol tolerance)", numberInput(payload.matcher_eps, { step: "any" }));
  epsField.style.display = payload.matcher_type === "float_tol" ? "" : "none";

  const ignoreCaseInput = el("input", { type: "checkbox", checked: payload.matcher_ignore_case });
  const ignoreWhitespaceInput = el("input", { type: "checkbox", checked: payload.matcher_ignore_whitespace });
  const ignorePunctuationInput = el("input", { type: "checkbox", checked: payload.matcher_ignore_punctuation });
  const allowExtraOutputInput = el("input", { type: "checkbox", checked: payload.matcher_allow_extra_output });
  const symbolGroupsInput = textInput(payload.matcher_symbol_groups, { placeholder: "x,X,*|:,=" });
  const toleranceFieldsWrap = el(
    "div",
    {},
    field("ignore case -- \"PASS\" matches \"pass\"", ignoreCaseInput),
    field("ignore whitespace -- \"root1 = 2.00\" matches \"root1=2.00\" (strips ALL whitespace, not just extra spaces)", ignoreWhitespaceInput),
    field("ignore punctuation -- \"INVALID\" matches \"INVALID:\" (strips . , : ; ! ? ' \")", ignorePunctuationInput),
    field(
      "allow extra output -- tolerates an unflushed prompt printed right before scanf ending up on the same line as the real output, e.g. actual \"Enter marks: PASS\" matches expected \"PASS\". Matches if expected appears ANYWHERE in actual, so only enable where that's an acceptable risk.",
      allowExtraOutputInput
    ),
    field(
      "symbol groups -- independent groups of interchangeable symbols, each collapsed to that group's first symbol; groups separated by |, symbols within a group by , (e.g. \"x,X,*|:,=\" treats x/X/* as interchangeable and, separately, :/= as interchangeable)",
      symbolGroupsInput
    )
  );
  toleranceFieldsWrap.style.display = payload.matcher_type === "custom" ? "none" : "";

  const matcherRow = radioRow("matcher_type", META.matcher_types, payload.matcher_type, (ev) => {
    epsField.style.display = ev.target.value === "float_tol" ? "" : "none";
    toleranceFieldsWrap.style.display = ev.target.value === "custom" ? "none" : "";
  });
  const matcherSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Matcher"),
    field("type", matcherRow),
    epsField,
    toleranceFieldsWrap
  );

  // Input handling
  const adjustCharInputInput = el("input", { type: "checkbox", checked: payload.adjust_char_input });
  const adjustScanfAddressInput = el("input", { type: "checkbox", checked: payload.adjust_scanf_address });
  const inputHandlingSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Input handling"),
    field(
      "adjust char input -- tolerates the classic missing-space-before-%c scanf mistake " +
        "(and a stray comma in a scanf format string) by compiling a corrected copy of the " +
        "submission for grading; the mistake is still noted in the student's report",
      adjustCharInputInput
    ),
    field(
      "adjust scanf address -- tolerates scanf(\"%d\", n) instead of scanf(\"%d\", &n) for a plain " +
        "scalar variable (never for %s/an array, and never for anything already a pointer) by " +
        "compiling a corrected copy of the submission for grading; the mistake is still noted in " +
        "the student's report",
      adjustScanfAddressInput
    )
  );

  // Code checks
  const ccEnabled = el("input", { type: "checkbox", checked: payload.code_checks_enabled });
  const ccFieldsWrap = el("div", {});
  ccFieldsWrap.style.display = payload.code_checks_enabled ? "" : "none";
  ccEnabled.addEventListener("change", () => {
    ccFieldsWrap.style.display = ccEnabled.checked ? "" : "none";
  });

  const marksField = field("marks (awarded if satisfied)", numberInput(payload.code_checks_marks, { step: "any" }));
  const penaltyField = field(
    "penalty (deducted from test marks if violated)",
    numberInput(payload.code_checks_penalty, { step: "any" })
  );
  const lineGateArea = textarea(payload.code_checks_line_gate_text, { class: "code", rows: 4 });
  const lineGateField = field(
    "line_gate mapping -- one \"index: construct\" per line (0-based index into a partial-credit test's " +
      "expected output; construct must also be listed in require above), e.g. \"0: bitwise_and\"",
    lineGateArea
  );
  const updateModeFields = (mode) => {
    marksField.style.display = mode === "bonus" ? "" : "none";
    penaltyField.style.display = mode === "penalty" ? "" : "none";
    lineGateField.style.display = mode === "line_gate" ? "" : "none";
  };
  updateModeFields(payload.code_checks_mode);
  const modeRow = radioRow(
    "code_checks_mode",
    ["gate", "bonus", "penalty", "line_gate"],
    payload.code_checks_mode,
    (ev) => updateModeFields(ev.target.value)
  );

  const requireGrid = checkboxGrid(
    "require-builtin",
    META.builtin_constructs,
    payload.code_checks_require_builtin
  );
  const requireExtra = textInput(payload.code_checks_require_extra, {
    placeholder: "e.g. function_call:foo, regex:\\bbar\\b",
  });
  const requireMatchRow = radioRow("code_checks_require_match", ["any", "all"], payload.code_checks_require_match);
  const forbidGrid = checkboxGrid("forbid-builtin", META.builtin_constructs, payload.code_checks_forbid_builtin);
  const forbidExtra = textInput(payload.code_checks_forbid_extra, {
    placeholder: "e.g. function_call:toupper, function_call:tolower",
  });

  ccFieldsWrap.append(
    field("mode", modeRow),
    marksField,
    penaltyField,
    lineGateField,
    field("require (checked constructs must appear in the code)", requireGrid),
    field("require: additional (function_call:.../regex:...)", requireExtra),
    field(
      "require match -- \"any\": at least one of the above must appear (default). \"all\": every one must appear.",
      requireMatchRow
    ),
    field("forbid (checked constructs must NOT appear in the code -- any one found is always a violation)", forbidGrid),
    field("forbid: additional (function_call:.../regex:...)", forbidExtra)
  );

  const codeChecksSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Code checks"),
    field("enable code_checks for this question", ccEnabled),
    ccFieldsWrap
  );

  // Gold solution
  const goldArea = textarea(payload.gold_c, { class: "code", rows: 12 });
  const goldSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Gold solution (gold.c)"),
    field("gold.c", goldArea)
  );

  // Tests
  const openList = el("div", {});
  (payload.tests_open || []).forEach((t) => openList.append(buildTestCase("open", t)));
  const hiddenList = el("div", {});
  (payload.tests_hidden || []).forEach((t) => hiddenList.append(buildTestCase("hidden", t)));

  const testsSection = el(
    "details",
    { name: "qsections" },
    el("summary", {}, el("span", { class: "arrow" }), "Test cases"),
    el(
      "details",
      { name: "test-groups" },
      el(
        "summary",
        {},
        el("span", { class: "arrow" }),
        "Open tests",
        el(
          "button",
          {
            type: "button",
            class: "link small-btn",
            onclick: (ev) => {
              ev.preventDefault();
              openList.append(buildTestCase("open", { name: "", in: "", out: "", weight: 0 }));
            },
          },
          "+ Add test case"
        )
      ),
      openList
    ),
    el(
      "details",
      { name: "test-groups" },
      el(
        "summary",
        {},
        el("span", { class: "arrow" }),
        "Hidden tests",
        el(
          "button",
          {
            type: "button",
            class: "link small-btn",
            onclick: (ev) => {
              ev.preventDefault();
              hiddenList.append(buildTestCase("hidden", { name: "", in: "", out: "", weight: 0 }));
            },
          },
          "+ Add test case"
        )
      ),
      hiddenList
    )
  );

  // Actions
  const saveBtn = el("button", { type: "submit" }, "Save");
  const deleteBtn = isNew
    ? null
    : el(
        "button",
        {
          type: "button",
          onclick: () => onDeleteQuestion(labId, qid),
        },
        "Delete"
      );
  const actions = el("div", { class: "actions" }, saveBtn, deleteBtn || el("span", {}));

  form.append(
    errorBox,
    basicInfo,
    marksSection,
    compileSection,
    limitsSection,
    matcherSection,
    inputHandlingSection,
    codeChecksSection,
    goldSection,
    testsSection,
    actions
  );

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    errorBox.innerHTML = "";

    const newId = idInput.value.trim();
    const outPayload = {
      id: isNew ? newId : payload.id,
      title: titleInput.value.trim(),
      description: descArea.value,
      filename_patterns: patternsArea.value.split("\n").map((s) => s.trim()).filter(Boolean),
      marks_open: parseFloat(marksOpenInput.value || "0"),
      marks_hidden: parseFloat(marksHiddenInput.value || "0"),
      compile_standard: form.querySelector('input[name="compile_standard"]:checked').value,
      compile_flags_common: Array.from(
        form.querySelectorAll('input[data-group="compile_flags"]:checked')
      ).map((i) => i.value),
      compile_flags_extra: flagsExtra.value,
      limits: {
        time_seconds: parseFloat(timeInput.value || "0"),
        wall_seconds: parseFloat(wallInput.value || "0"),
        memory_mb: parseInt(memInput.value || "0", 10),
        output_bytes: parseInt(outputBytesInput.value || "0", 10),
        max_processes: parseInt(maxProcInput.value || "1", 10),
      },
      matcher_type: form.querySelector('input[name="matcher_type"]:checked').value,
      matcher_eps: numOrNull(epsField.querySelector("input")),
      matcher_ignore_case: ignoreCaseInput.checked,
      matcher_ignore_whitespace: ignoreWhitespaceInput.checked,
      matcher_ignore_punctuation: ignorePunctuationInput.checked,
      matcher_allow_extra_output: allowExtraOutputInput.checked,
      matcher_symbol_groups: symbolGroupsInput.value,
      adjust_char_input: adjustCharInputInput.checked,
      adjust_scanf_address: adjustScanfAddressInput.checked,
      code_checks_enabled: ccEnabled.checked,
      code_checks_mode: form.querySelector('input[name="code_checks_mode"]:checked').value,
      code_checks_require_builtin: Array.from(
        form.querySelectorAll('input[data-group="require-builtin"]:checked')
      ).map((i) => i.value),
      code_checks_require_extra: requireExtra.value,
      code_checks_require_match: form.querySelector('input[name="code_checks_require_match"]:checked').value,
      code_checks_forbid_builtin: Array.from(
        form.querySelectorAll('input[data-group="forbid-builtin"]:checked')
      ).map((i) => i.value),
      code_checks_forbid_extra: forbidExtra.value,
      code_checks_marks: numOrNull(marksField.querySelector("input")),
      code_checks_penalty: numOrNull(penaltyField.querySelector("input")),
      code_checks_line_gate_text: lineGateArea.value,
      tests_open: gatherTestCases(form, "open"),
      tests_hidden: gatherTestCases(form, "hidden"),
      gold_c: goldArea.value,
    };

    if (!outPayload.id) {
      errorBox.append(el("div", { class: "error-msg" }, "id is required."));
      return;
    }

    try {
      await api(`/api/labs/${encodeURIComponent(labId)}/questions/${encodeURIComponent(outPayload.id)}`, {
        method: "POST",
        body: JSON.stringify(outPayload),
      });
      document.getElementById("question-modal").close();
      await loadTree();
    } catch (e) {
      errorBox.innerHTML = "";
      errorBox.append(el("div", { class: "error-msg" }, `Save failed: ${e.message}`));
    }
  });

  return form;
}

async function onDeleteQuestion(labId, qid) {
  if (!confirm(`Delete question "${qid}"? This cannot be undone.`)) return;
  try {
    await api(`/api/labs/${encodeURIComponent(labId)}/questions/${encodeURIComponent(qid)}`, {
      method: "DELETE",
    });
    document.getElementById("question-modal").close();
    await loadTree();
  } catch (e) {
    alert(`Could not delete question: ${e.message}`);
  }
}

/* ---------------------------------------------------------------------- */
/* init                                                                     */
/* ---------------------------------------------------------------------- */

document.querySelectorAll("dialog [data-close]").forEach((btn) => {
  btn.addEventListener("click", () => btn.closest("dialog").close());
});

(async function init() {
  META = await api("/api/meta");
  await loadTree();
})();

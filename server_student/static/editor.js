(() => {
  "use strict";

  const myRollNo = document.getElementById("app").dataset.rollNo;

  const timerEl = document.getElementById("timer");
  const bannerEl = document.getElementById("banner");
  const waitingView = document.getElementById("waiting-view");
  const workspaceView = document.getElementById("workspace-view");
  const reportView = document.getElementById("report-view");
  const leaderboardView = document.getElementById("leaderboard-view");
  const nothingView = document.getElementById("nothing-view");
  const questionListEl = document.getElementById("question-list");
  const qTitleEl = document.getElementById("q-title");
  const qDescEl = document.getElementById("q-description");
  const runBtn = document.getElementById("run-btn");
  const saveStatusEl = document.getElementById("save-status");
  const resultsPanelEl = document.getElementById("results-panel");
  const logoutBtn = document.getElementById("logout-btn");
  const sharedFilesMenuEl = document.getElementById("shared-files-menu");
  const sharedFilesListEl = document.getElementById("shared-files-list");
  let lastRenderedSharedFiles = null; // JSON of the last list actually rendered, so pollStatus() only touches the DOM (and closes an open menu) when the list actually changed
  const postSessionNavEl = document.getElementById("post-session-nav");

  let heartbeatIntervalSeconds = 60;
  let questions = [];
  let currentQid = null;
  let dirty = false;
  let writeAllowed = false;
  let saving = false;
  // The full result of the last Run for the *currently open* question (or
  // null if it's never been run), plus whether the editor's live content
  // has since diverged from the exact source that produced it. Restored
  // from the server on selectQuestion (LAST_RUN there survives switching
  // questions and even a page reload) and flipped to stale the moment the
  // student types anything, without waiting for a round trip -- the server
  // only finds out (and would agree) once the next save/run happens.
  let currentRunResult = null;
  let currentRunStale = false;

  // Persistent save/run status line (next to the Run button) -- sourced
  // from the server (file mtime for "saved", the stored run record's own
  // timestamp for "run"), not just an ephemeral string set after an action,
  // so it survives a page reload or switching questions and back.
  let lastSavedAt = null; // ISO string or null
  let lastRunAt = null; // ISO string or null

  // Server-authoritative session state, refreshed once per heartbeat (see
  // below) -- the on-screen countdown itself ticks locally every second
  // between heartbeats via tickTimer() so it stays smooth without needing
  // a network request every second from every logged-in student.
  let sessionStatus = "not_started";
  let timeRemainingSeconds = 0;
  let resyncing = false;
  let fiveMinuteWarningShown = false;
  let studentLocked = false;
  let studentLockAlertShown = false;
  let studentUnlockAlertShown = false;

  // Server-side toggles (see /api/config) for what stays visible once the
  // session is finalized -- workspace browsing, report, and leaderboard
  // can each be independently turned off by the instructor, *at runtime*
  // (grader.display_config, not a fixed CLI flag) -- so this is re-fetched
  // every heartbeat, not just once at load, and a change is reflected
  // (including hiding/flushing already-fetched content) within one
  // heartbeat interval. See loadConfig()/applyConfigChange() below.
  let CONFIG = { show_workspace_after_session: true, show_report: true, show_leaderboard: true };
  // Which top-level post-session view is showing. Default priority is
  // workspace > report > leaderboard (see availablePostSessionViews and
  // pollStatus's "finalized" branch, which falls back to avail[0] --
  // built in this same order -- whenever the current selection isn't
  // available, e.g. on first arrival or after an admin disables it).
  let postSessionView = "workspace";
  let reportLoaded = false;
  let leaderboardLoaded = false;

  function availablePostSessionViews() {
    const views = [];
    if (CONFIG.show_workspace_after_session) views.push("workspace");
    if (CONFIG.show_report) views.push("report");
    if (CONFIG.show_leaderboard) views.push("leaderboard");
    return views;
  }

  const cm = CodeMirror.fromTextArea(document.getElementById("code-editor"), {
    mode: "text/x-csrc",
    lineNumbers: true,
    indentUnit: 4,
    tabSize: 4,
    indentWithTabs: false,
    matchBrackets: true,
    theme: "default",
  });
  setTimeout(() => cm.refresh(), 100);
  cm.on("change", (instance, changeObj) => {
    // cm.setValue() (used when switching questions) fires a change event
    // too, with origin "setValue" -- must not count as a real edit, or
    // switching to a question would immediately mark its own just-loaded,
    // untouched result as stale.
    if (changeObj.origin === "setValue") return;
    dirty = true;
    renderSaveStatus();
    if (currentRunResult && !currentRunStale) {
      currentRunStale = true;
      renderRunResult(currentRunResult, true);
      markActiveChipStale();
    }
  });

  function fmtTime(totalSeconds) {
    totalSeconds = Math.max(0, Math.round(totalSeconds));
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  // Ticks the on-screen countdown once a second locally, between the
  // (infrequent, server-authoritative) heartbeat polls -- so the timer
  // still looks smooth with 30+ students logged in without each of them
  // hitting the server every second. The moment the local countdown
  // reaches zero, it triggers one immediate resync (rather than waiting up
  // to a full heartbeat interval) so the lock takes effect promptly.
  function tickTimer() {
    if (sessionStatus !== "running") return;
    timeRemainingSeconds = Math.max(0, timeRemainingSeconds - 1);
    timerEl.textContent = fmtTime(timeRemainingSeconds);
    if (timeRemainingSeconds <= 300 && timeRemainingSeconds > 299 && !fiveMinuteWarningShown) {
      fiveMinuteWarningShown = true;
      alert("⏰ Session ending in 5 minutes!\n\nYou will not be able to submit any code once the timer reaches 00:00.\n\nPlease:\n• Finish your work as soon as possible\n• Run your code to save the results\n• Save any unsaved changes\n\nMake sure everything is submitted before time runs out!");
    }
    if (timeRemainingSeconds <= 0 && !resyncing) {
      resyncing = true;
      pollStatus().finally(() => {
        resyncing = false;
      });
    }
  }
  setInterval(tickTimer, 1000);

  function updateCodeEditorLockStatus() {
    if (studentLocked) {
      runBtn.disabled = true;
      cm.setOption("readOnly", "nocursor");
      saveStatusEl.textContent = "Account locked - cannot submit";
    } else if (!writeAllowed) {
      runBtn.disabled = true;
      cm.setOption("readOnly", "nocursor");
      renderSaveStatus();
    } else {
      runBtn.disabled = false;
      cm.setOption("readOnly", false);
      renderSaveStatus();
    }
  }

  function renderSaveStatus() {
    if (!writeAllowed) {
      saveStatusEl.textContent = "Read-only (lab is not accepting submissions)";
      return;
    }
    if (studentLocked) {
      saveStatusEl.textContent = "Account locked - cannot submit";
      return;
    }
    const parts = [];
    if (dirty) parts.push("Unsaved changes");
    if (lastSavedAt) parts.push("Saved " + new Date(lastSavedAt).toLocaleTimeString());
    if (lastRunAt) parts.push("Last run " + new Date(lastRunAt).toLocaleTimeString());
    saveStatusEl.textContent = parts.length ? parts.join(" · ") : "Not saved yet";
  }

  function showBanner(text) {
    if (!text) {
      bannerEl.hidden = true;
      return;
    }
    bannerEl.textContent = text;
    bannerEl.hidden = false;
  }

  function showView(name) {
    waitingView.hidden = name !== "waiting";
    workspaceView.hidden = name !== "workspace";
    reportView.hidden = name !== "report";
    leaderboardView.hidden = name !== "leaderboard";
    nothingView.hidden = name !== "nothing";
  }

  function updatePostNavButtons() {
    document.querySelectorAll(".post-nav-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === postSessionView);
    });
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    let data = {};
    try {
      data = await res.json();
    } catch (e) {
      /* no body */
    }
    if (res.status === 401) {
      // The server re-checks the account's currently-bound device on every
      // request (see server_student.app._current_roll_no) -- a 401 here
      // means either we were never logged in, or this session's device was
      // just unbound/superseded by another device. Either way, the signed
      // session cookie itself is now dead, so force back to the login page
      // rather than letting the UI keep rendering stale state.
      window.location.href = "/login?reason=device_changed";
    }
    return { ok: res.ok, status: res.status, data };
  }

  async function doSave(qidOverride) {
    const qid = qidOverride || currentQid;
    if (!qid || !writeAllowed || saving || studentLocked) return;
    saving = true;
    const source = cm.getValue();
    const { ok, data } = await api("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ qid, source }),
    });
    saving = false;
    if (ok) {
      dirty = false;
      lastSavedAt = data.saved_at || new Date().toISOString();
      renderSaveStatus();
    } else {
      saveStatusEl.textContent = data.error || "save failed";
    }
  }

  // Background autosave insurance against losing work if a student edits
  // but never clicks Run before the timer expires (LIVE_LAB_DESIGN.md §9)
  // is folded into the heartbeat() function below (one combined request
  // per interval, not a separate timer) -- always saves on Run too, and
  // once more on tab-switch/close right here via sendBeacon.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && dirty && currentQid && writeAllowed) {
      navigator.sendBeacon(
        "/api/save",
        new Blob([JSON.stringify({ qid: currentQid, source: cm.getValue() })], { type: "application/json" })
      );
    }
  });
  window.addEventListener("beforeunload", () => {
    if (dirty && currentQid && writeAllowed) {
      navigator.sendBeacon(
        "/api/save",
        new Blob([JSON.stringify({ qid: currentQid, source: cm.getValue() })], { type: "application/json" })
      );
    }
  });

  function renderQuestionList() {
    questionListEl.innerHTML = "";
    for (const q of questions) {
      const item = document.createElement("div");
      item.className = "q-item" + (q.qid === currentQid ? " active" : "");
      let chip = "not attempted";
      let chipClass = "chip-neutral";
      if (q.last_run) {
        chip = `${q.last_run.open_passed}/${q.last_run.open_total} open tests`;
        const allPassed = q.last_run.open_passed === q.last_run.open_total;
        // A code-checks violation (e.g. the question required a switch
        // statement and this submission didn't use one) means the student
        // hasn't actually done what the question asked, even if every test
        // passed byte-for-byte -- that's still a "look again" state, not a
        // clean pass, so it stays orange rather than green.
        const codeCheckFailed = q.last_run.code_check_satisfied === false;
        if (!q.last_run.compile_ok) {
          chipClass = "chip-bad";
        } else if (q.last_run_stale) {
          chipClass = "chip-warn";
          chip += " · stale";
        } else if (codeCheckFailed) {
          chipClass = "chip-warn";
        } else if (allPassed) {
          chipClass = "chip-good";
        } else {
          chipClass = "chip-warn";
        }
      } else if (q.has_saved_code) {
        chip = "saved, not run";
      }
      item.innerHTML = `<div class="q-item-title">${q.qid}</div>
        <div class="q-item-sub">${q.title}</div>
        <div class="chip ${chipClass}">${chip}</div>`;
      item.addEventListener("click", () => selectQuestion(q.qid));
      questionListEl.appendChild(item);
    }
  }

  async function selectQuestion(qid) {
    if (currentQid && dirty) await doSave();
    currentQid = qid;
    renderQuestionList();
    const { ok, data } = await api(`/api/questions/${encodeURIComponent(qid)}`);
    if (!ok) {
      qTitleEl.textContent = data.error || "Could not load question";
      return;
    }
    qTitleEl.textContent = `${data.qid}: ${data.title}`;
    qDescEl.textContent = data.description;
    currentRunResult = data.last_run || null;
    currentRunStale = data.last_run ? !!data.last_run_stale : false;
    lastSavedAt = data.saved_at || null;
    lastRunAt = (data.last_run && data.last_run.run_at) || null;
    cm.setValue(data.source || "");
    dirty = false;
    writeAllowed = data.write_allowed;
    updateCodeEditorLockStatus();
    renderSaveStatus();
    renderRunResult(currentRunResult, currentRunStale);
    setTimeout(() => cm.refresh(), 50);
  }

  function renderTestResult(d) {
    let label = d.passed ? "PASS" : "FAIL";
    if (d.passed && d.pass_reasons && d.pass_reasons.length) {
      label = `PASS (${d.pass_reasons.join(", ")})`;
    } else if (!d.passed && d.partial_earned !== null && d.partial_earned !== undefined) {
      label = `PARTIAL (${d.lines_matched}/${d.lines_total} lines, +${d.partial_earned}/${d.weight} marks)`;
    } else if (!d.passed && d.status !== "OK") {
      label = `FAIL (${d.status}${d.message ? ": " + d.message : ""})`;
    }
    const cls = d.passed ? "test-pass" : d.partial_earned ? "test-partial" : "test-fail";
    const showDetail = !d.passed || (d.pass_reasons && d.pass_reasons.length);
    return `<div class="test-row ${cls}">
      <div class="test-head"><code>${d.name}</code>: ${label}</div>
      ${
        showDetail
          ? `<div class="test-io">
              <div><span>input</span><pre>${escapeHtml(d.input)}</pre></div>
              <div><span>expected</span><pre>${escapeHtml(d.expected)}</pre></div>
              <div><span>actual</span><pre>${escapeHtml(d.actual)}</pre></div>
            </div>`
          : ""
      }
    </div>`;
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function renderCodeCheck(cc) {
    if (!cc) return "";
    let lines = [`<div class="code-check">`, `<strong>Code checks (${cc.mode}):</strong> ${cc.satisfied ? "PASSED" : "FAILED"}`];
    if (cc.found.length) {
      lines.push(
        "<ul>" + cc.found.map(([name, line]) => `<li><code>${name}</code>: found (line ${line})</li>`).join("") + "</ul>"
      );
    }
    if (cc.missing.length) {
      lines.push("<ul>" + cc.missing.map((name) => `<li><code>${name}</code>: <b>not found</b></li>`).join("") + "</ul>");
    }
    if (cc.forbidden.length) {
      lines.push(
        "<ul>" +
          cc.forbidden.map(([name, line]) => `<li>forbidden <code>${name}</code>: found at line ${line}</li>`).join("") +
          "</ul>"
      );
    }
    if (cc.gated_to_zero) {
      lines.push(`<div class="warn">All marks for this question are zeroed due to a gate violation.</div>`);
    }
    if (cc.mode === "penalty" && cc.penalty_applied > 0) {
      lines.push(`<div class="warn">Penalty applied: -${cc.penalty_applied} marks.</div>`);
    }
    lines.push("</div>");
    return lines.join("");
  }

  function staleBannerHtml() {
    return `<div class="stale-banner">Your code has changed since this was last run — these results may be out of date. Click Run to refresh.</div>`;
  }

  // Shared by selectQuestion (restoring a cached result when switching back
  // to a question) and the Run button handler (showing a fresh result) --
  // one rendering path so the two can never drift apart.
  function renderRunResult(data, stale) {
    if (!data) {
      resultsPanelEl.innerHTML = `<div class="hint">Not run yet for this question.</div>`;
      return;
    }
    if (!data.compile_ok) {
      resultsPanelEl.innerHTML =
        (stale ? staleBannerHtml() : "") +
        `<div class="test-row test-fail">
          <div class="test-head">Compile failed</div>
          <pre>${escapeHtml(data.compile_stderr)}</pre>
        </div>`;
      return;
    }
    const codeCheckFailed = data.code_check && data.code_check.satisfied === false;
    const marksClass = "marks-line" + (codeCheckFailed || stale ? " marks-warn" : "");
    let html = stale ? staleBannerHtml() : "";
    html += `<div class="${marksClass}">Open tests: ${data.marks_earned}/${data.marks_total}</div>`;
    html += data.tests.map(renderTestResult).join("");
    html += renderCodeCheck(data.code_check);
    resultsPanelEl.innerHTML = html;
  }

  // Live, no-round-trip feedback for the question currently being edited.
  // Mutates the `questions` array itself (not just the DOM) -- renderQuestionList
  // rebuilds the whole sidebar from that array on every call, including when
  // switching to a *different* question, so a DOM-only patch here would get
  // silently discarded the moment the student navigated away: the rebuild
  // would fall back to whatever last_run_stale the last /api/questions
  // fetch had, which predates this edit and would wrongly show green again.
  // Full correctness (including for every *other* question) is still
  // reconciled from the server the next time refreshQuestions() runs.
  function markActiveChipStale() {
    const entry = questions.find((q) => q.qid === currentQid);
    if (entry && entry.last_run) {
      entry.last_run_stale = true;
      renderQuestionList();
    }
  }

  runBtn.addEventListener("click", async () => {
    if (!currentQid || !writeAllowed || studentLocked) return;
    runBtn.disabled = true;
    runBtn.textContent = "Running...";
    resultsPanelEl.innerHTML = `<div class="hint">Compiling and running against open tests...</div>`;
    const { ok, status, data } = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ qid: currentQid, source: cm.getValue() }),
    });
    runBtn.disabled = false;
    runBtn.textContent = "Run";

    if (!ok) {
      // The server only actually saves once the cooldown/lock checks pass
      // (see server_student.app.api_run) -- a rejected Run (429/423/503)
      // may not have saved anything, so dirty/lastSavedAt must NOT be
      // touched here, or the status line would claim a save that didn't
      // happen.
      resultsPanelEl.innerHTML = `<div class="test-row test-fail">${escapeHtml(data.error || `Run failed (${status})`)}</div>`;
      return;
    }

    dirty = false;
    lastSavedAt = data.run_at;
    lastRunAt = data.run_at;
    renderSaveStatus();
    currentRunResult = data;
    currentRunStale = false;
    renderRunResult(data, false);
    await refreshQuestions();
  });

  function renderSharedFiles(files) {
    const asJson = JSON.stringify(files);
    if (asJson === lastRenderedSharedFiles) return;
    lastRenderedSharedFiles = asJson;

    sharedFilesMenuEl.hidden = files.length === 0;
    if (files.length === 0) {
      sharedFilesMenuEl.open = false; // closing an already-empty menu is a no-op, but guards the case where files disappeared while the menu was open
      return;
    }
    sharedFilesListEl.innerHTML = "";
    for (const name of files) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      // Session cookie rides along automatically -- the browser handles
      // the download/inline-view itself, no need to route this through
      // api(). target="_blank" so it opens/downloads without navigating
      // away from the editor (and losing unsaved code).
      a.href = `/api/shared-files/${encodeURIComponent(name)}`;
      a.target = "_blank";
      a.textContent = name;
      li.appendChild(a);
      sharedFilesListEl.appendChild(li);
    }
  }

  logoutBtn.addEventListener("click", async () => {
    if (currentQid && dirty) await doSave();
    await api("/api/logout", { method: "POST" });
    window.location.href = "/login";
  });

  async function refreshQuestions() {
    const { ok, data } = await api("/api/questions");
    if (!ok) return;
    questions = data.questions;
    renderQuestionList();
  }

  // Clears already-fetched content for whichever view(s) just got disabled
  // (comparing against the previous config), so a later re-enable can't
  // show stale data and definitely triggers a fresh fetch (loaded flags
  // reset here). Workspace content isn't really "someone else's data" the
  // way report/leaderboard could be seen as, but is flushed the same way
  // for consistency and so a disabled-then-re-enabled workspace always
  // reloads cleanly rather than showing whatever was left in the editor.
  function flushDisabledContent(previous) {
    if (previous.show_report && !CONFIG.show_report) {
      document.getElementById("report-tab").innerHTML = "";
      reportLoaded = false;
    }
    if (previous.show_leaderboard && !CONFIG.show_leaderboard) {
      document.getElementById("leaderboard-tab").innerHTML = "";
      leaderboardLoaded = false;
    }
    if (previous.show_workspace_after_session && !CONFIG.show_workspace_after_session) {
      questionListEl.innerHTML = "";
      qTitleEl.textContent = "Select a question";
      qDescEl.textContent = "";
      cm.setValue("");
      resultsPanelEl.innerHTML = "";
      currentQid = null;
      currentRunResult = null;
      currentRunStale = false;
      questions = [];
    }
  }

  // Re-fetched every heartbeat while finalized (not just once) -- these
  // toggles are runtime-editable by the admin (grader.display_config), so
  // a change must be reflected within one heartbeat interval, in both
  // directions (hide+flush on disable, fetch+show on enable).
  async function loadConfig() {
    const cfg = await api("/api/config");
    if (!cfg.ok) return;
    heartbeatIntervalSeconds = cfg.data.heartbeat_interval_seconds;
    const previous = CONFIG;
    CONFIG = {
      show_workspace_after_session: cfg.data.show_workspace_after_session,
      show_report: cfg.data.show_report,
      show_leaderboard: cfg.data.show_leaderboard,
    };
    flushDisabledContent(previous);
  }

  async function ensureViewLoaded(view) {
    if (view === "report" && CONFIG.show_report && !reportLoaded) {
      const r = await api("/api/results");
      document.getElementById("report-tab").innerHTML = r.ok
        ? r.data.html
        : `<p class="hint">${escapeHtml(r.data.error || "No report available.")}</p>`;
      reportLoaded = true;
    } else if (view === "leaderboard" && CONFIG.show_leaderboard && !leaderboardLoaded) {
      const lb = await api("/api/leaderboard");
      const tab = document.getElementById("leaderboard-tab");
      if (lb.ok) {
        const rows = lb.data.leaderboard
          .map((row) => {
            const mine = row.roll_no === myRollNo ? ' class="me-row"' : "";
            return `<tr${mine}><td>${row.rank}</td><td>${row.name || "-"}</td><td>${row.roll_no}</td><td>${row.earned}/${row.possible}</td></tr>`;
          })
          .join("");
        tab.innerHTML = `<table class="leaderboard-table"><thead><tr><th>#</th><th>Name</th><th>Roll no.</th><th>Total</th></tr></thead><tbody>${rows}</tbody></table>`;
      } else {
        tab.innerHTML = `<p class="hint">${escapeHtml(lb.data.error || "No leaderboard available.")}</p>`;
      }
      leaderboardLoaded = true;
    }
  }

  document.querySelectorAll(".post-nav-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      postSessionView = btn.dataset.view;
      updatePostNavButtons();
      await ensureViewLoaded(postSessionView);
      showView(postSessionView);
    });
  });

  async function pollStatus() {
    const { ok, data } = await api("/api/session/status");
    if (!ok) return;
    sessionStatus = data.status;
    renderSharedFiles(data.shared_files || []);

    const newLockedStatus = data.student_locked || false;
    if (newLockedStatus && !studentLocked && !studentLockAlertShown) {
      studentLockAlertShown = true;
      alert("⛔ Your account has been locked by the instructor.\n\nYou can no longer save or run code. You can still view questions and results.\n\nPlease contact your instructor if you have questions.");
    } else if (!newLockedStatus && studentLocked && !studentUnlockAlertShown) {
      studentUnlockAlertShown = true;
      alert("✅ Your account has been unlocked.\n\nYou can now save and run code again.");
    }
    studentLocked = newLockedStatus;
    updateCodeEditorLockStatus();

    if (data.status === "not_started") {
      showView("waiting");
      postSessionNavEl.hidden = true;
      showBanner(null);
      timerEl.textContent = "--:--";
      return;
    }
    if (data.status === "running") {
      timeRemainingSeconds = data.time_remaining_seconds;
      timerEl.textContent = fmtTime(timeRemainingSeconds);
      postSessionNavEl.hidden = true;
      if (workspaceView.hidden) {
        showView("workspace");
        await refreshQuestions();
      }
      showBanner(null);
      return;
    }
    if (data.status === "locked") {
      timerEl.textContent = "0:00";
      postSessionNavEl.hidden = true;
      showView("workspace");
      showBanner("Time is up. Your code has been locked and is being graded.");
      writeAllowed = false;
      runBtn.disabled = true;
      cm.setOption("readOnly", "nocursor");
      if (currentQid) renderSaveStatus();
      return;
    }
    if (data.status === "finalized") {
      timerEl.textContent = "Over";
      await loadConfig();
      showBanner(null);

      // Workspace content re-populates itself if it just became available
      // again after being disabled+flushed earlier.
      if (CONFIG.show_workspace_after_session && questions.length === 0) {
        await refreshQuestions();
      }

      const avail = availablePostSessionViews();
      if (avail.length === 0) {
        postSessionNavEl.hidden = true;
        showView("nothing");
        return;
      }

      postSessionNavEl.hidden = false;
      document.querySelectorAll(".post-nav-btn").forEach((btn) => {
        btn.hidden = !avail.includes(btn.dataset.view);
      });
      if (!avail.includes(postSessionView)) postSessionView = avail[0];
      updatePostNavButtons();
      await ensureViewLoaded(postSessionView);
      showView(postSessionView);
    }
  }

  // One combined request per interval instead of a separate status-poll
  // timer and autosave timer -- with 30+ students logged in simultaneously,
  // halving the steady-state request count matters. tickTimer() (1s, purely
  // local) is what keeps the on-screen countdown smooth in between.
  async function heartbeat() {
    await pollStatus();
    if (dirty && currentQid && writeAllowed) {
      await doSave();
    }
  }

  async function init() {
    await loadConfig();
    await pollStatus();
    setInterval(heartbeat, heartbeatIntervalSeconds * 1000);
  }

  init();
})();

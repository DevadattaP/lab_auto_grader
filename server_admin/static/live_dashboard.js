(() => {
  "use strict";

  const appEl = document.getElementById("app");
  const labId = appEl.dataset.labId;

  const statusPillEl = document.getElementById("status-pill");
  const headerTimerEl = document.getElementById("header-timer");
  const sessionStatusBoxEl = document.getElementById("session-status-box");
  const sessionMsgEl = document.getElementById("session-msg");
  const finalizeBtn = document.getElementById("finalize-btn");
  const finalizeMsgEl = document.getElementById("finalize-msg");
  const finalizeOutputEl = document.getElementById("finalize-output");

  let currentStatus = "not_started";

  async function api(path, opts) {
    const res = await fetch(path, opts);
    let data = {};
    try {
      data = await res.json();
    } catch (e) {
      /* no body */
    }
    if (res.status === 401) {
      window.location.href = "/admin/login";
    }
    return { ok: res.ok, status: res.status, data };
  }

  function fmtTime(totalSeconds) {
    totalSeconds = Math.max(0, Math.round(totalSeconds));
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  }

  /* ---------------------------------------------------------------- tabs */

  document.querySelectorAll(".admin-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".admin-tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".admin-tab-panel").forEach((p) => (p.hidden = true));
      document.getElementById(`tab-${btn.dataset.tab}`).hidden = false;
      if (btn.dataset.tab === "accounts") loadAccounts();
      if (btn.dataset.tab === "live") loadLiveStatus();
    });
  });

  /* ------------------------------------------------------------- session */

  async function loadSessionStatus() {
    const { ok, data } = await api(`/api/live/${labId}/session/status`);
    if (!ok) return;
    currentStatus = data.status;

    statusPillEl.textContent = data.status.replace("_", " ");
    statusPillEl.className = `status-pill status-${data.status}`;
    headerTimerEl.textContent = data.status === "running" ? fmtTime(data.time_remaining_seconds) : "";

    const lines = [`Status: <strong>${data.status}</strong>`];
    if (data.start_time) lines.push(`Started: ${new Date(data.start_time).toLocaleString()}`);
    if (data.duration_minutes) lines.push(`Duration: ${data.duration_minutes} min`);
    if (data.end_time) lines.push(`Ends: ${new Date(data.end_time).toLocaleString()}`);
    if (data.locked_at) lines.push(`Locked at: ${new Date(data.locked_at).toLocaleString()}`);
    if (data.finalized_run) lines.push(`Finalized run: ${data.finalized_run}`);
    sessionStatusBoxEl.innerHTML = lines.join("<br>");

    finalizeBtn.disabled = data.status !== "locked";
  }

  document.getElementById("start-btn").addEventListener("click", async () => {
    const duration_minutes = parseInt(document.getElementById("start-duration").value, 10);
    const { ok, data } = await api(`/api/live/${labId}/session/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duration_minutes }),
    });
    sessionMsgEl.textContent = ok ? "Session started." : data.error || "Failed to start session.";
    await loadSessionStatus();
  });

  document.getElementById("extend-btn").addEventListener("click", async () => {
    const additional_minutes = parseInt(document.getElementById("extend-minutes").value, 10);
    const { ok, data } = await api(`/api/live/${labId}/session/extend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ additional_minutes }),
    });
    sessionMsgEl.textContent = ok ? "Session extended." : data.error || "Failed to extend session.";
    await loadSessionStatus();
  });

  document.getElementById("lock-btn").addEventListener("click", async () => {
    if (!confirm("Lock the session now? Students will no longer be able to save or run code.")) return;
    const { ok, data } = await api(`/api/live/${labId}/session/lock`, { method: "POST" });
    sessionMsgEl.textContent = ok ? "Session locked." : data.error || "Failed to lock session.";
    await loadSessionStatus();
  });

  document.getElementById("reset-btn").addEventListener("click", async () => {
    if (!confirm("Reset the session back to 'not started'? This does not touch student code, accounts, or past results.")) return;
    const { ok, data } = await api(`/api/live/${labId}/session/reset`, { method: "POST" });
    sessionMsgEl.textContent = ok ? "Session reset." : data.error || "Failed to reset session.";
    await loadSessionStatus();
  });

  /* ------------------------------------------------------- display config */

  const displayConfigMsgEl = document.getElementById("display-config-msg");
  const cfgWorkspaceEl = document.getElementById("cfg-workspace");
  const cfgReportEl = document.getElementById("cfg-report");
  const cfgLeaderboardEl = document.getElementById("cfg-leaderboard");

  async function loadDisplayConfig() {
    const { ok, data } = await api(`/api/live/${labId}/display-config`);
    if (!ok) return;
    cfgWorkspaceEl.checked = data.show_workspace_after_session;
    cfgReportEl.checked = data.show_report;
    cfgLeaderboardEl.checked = data.show_leaderboard;
  }

  document.getElementById("save-display-config-btn").addEventListener("click", async () => {
    const { ok, data } = await api(`/api/live/${labId}/display-config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        show_workspace_after_session: cfgWorkspaceEl.checked,
        show_report: cfgReportEl.checked,
        show_leaderboard: cfgLeaderboardEl.checked,
      }),
    });
    displayConfigMsgEl.textContent = ok
      ? "Saved -- students will pick this up within one heartbeat."
      : data.error || "Failed to save.";
  });

  /* ------------------------------------------------------------ accounts */

  async function loadAccounts() {
    const { ok, data } = await api(`/api/live/${labId}/accounts`);
    const tbody = document.querySelector("#accounts-table tbody");
    tbody.innerHTML = "";
    if (!ok || !data.accounts.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="hint">No students have signed in for this lab yet.</td></tr>`;
      return;
    }
    for (const a of data.accounts) {
      const tr = document.createElement("tr");
      // `active` is the real online/offline signal; `ip` is a persistent
      // "last device used" record that survives logout (kept for the
      // summary.csv report enrichment at Finalize) -- so a student who
      // has signed out still shows their last IP, just in red/offline
      // instead of green/online.
      const device = a.active
        ? `<span class="device-yes">online (${a.ip})</span>`
        : a.ip
          ? `<span class="device-offline">offline (last: ${a.ip})</span>`
          : `<span class="device-no">never signed in</span>`;
      const passwordStatus = a.password_set
        ? `<span class="device-yes">custom</span>`
        : `<span class="device-offline">default (not changed yet)</span>`;
      tr.innerHTML = `
        <td>${a.roll_no}</td>
        <td>${a.name || "-"}</td>
        <td>${passwordStatus}</td>
        <td>${device}</td>
        <td>${a.last_seen || "-"}</td>
        <td>
          <button type="button" data-action="reset">Reset password</button>
          <button type="button" data-action="unbind" ${a.active ? "" : "disabled"}>Sign out device</button>
        </td>`;
      tr.querySelector('[data-action="reset"]').addEventListener("click", () => resetAccount(a.roll_no));
      tr.querySelector('[data-action="unbind"]').addEventListener("click", () => unbindAccount(a.roll_no));
      tbody.appendChild(tr);
    }
  }

  async function resetAccount(roll_no) {
    const new_password = prompt(`New password for ${roll_no}:`);
    if (new_password === null) return; // cancelled
    const confirm_password = prompt("Confirm new password:");
    if (confirm_password === null) return;
    if (new_password !== confirm_password) {
      alert("Passwords do not match -- nothing was changed.");
      return;
    }
    const { ok, data } = await api(`/api/live/${labId}/accounts/${encodeURIComponent(roll_no)}/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: new_password, confirm_password }),
    });
    if (ok) {
      alert(`Password updated for ${roll_no}. Share it with them directly -- it will not be shown again here.`);
    } else {
      alert(data.error || "Failed to reset password.");
    }
    await loadAccounts();
  }

  async function unbindAccount(roll_no) {
    if (!confirm(`Unbind ${roll_no}'s device? Their current session will be signed out and they can log in from a new device.`)) return;
    const { ok, data } = await api(`/api/live/${labId}/accounts/${encodeURIComponent(roll_no)}/unbind`, { method: "POST" });
    if (!ok) alert(data.error || "Failed to unbind device.");
    await loadAccounts();
  }

  /* ---------------------------------------------------------- live status */

  function cellHtml(entry) {
    if (!entry) return { cls: "cell-neutral", text: "—" };
    if (!entry.compile_ok) return { cls: "cell-bad", text: "compile error" };
    if (entry.code_check_satisfied === false) return { cls: "cell-warn", text: `${entry.open_passed}/${entry.open_total}*` };
    if (entry.open_passed === entry.open_total) return { cls: "cell-good", text: `${entry.open_passed}/${entry.open_total}` };
    return { cls: "cell-warn", text: `${entry.open_passed}/${entry.open_total}` };
  }

  async function loadLiveStatus() {
    const { ok, data } = await api(`/api/live/${labId}/dashboard`);
    const wrap = document.getElementById("live-table-wrap");
    if (!ok) {
      wrap.innerHTML = `<p class="hint">${data.error || "Failed to load live status."}</p>`;
      return;
    }
    if (!data.students.length) {
      wrap.innerHTML = `<p class="hint">No accounts generated for this lab yet.</p>`;
      return;
    }
    const qids = data.question_ids;
    let html = `<table class="live-grid"><thead><tr><th class="roll-col">Roll no.</th><th class="roll-col">Name</th><th>Online</th>`;
    for (const qid of qids) html += `<th>${qid}</th>`;
    html += `<th>Total</th></tr></thead><tbody>`;
    for (const s of data.students) {
      html += `<tr><td class="roll-col">${s.roll_no}</td><td class="roll-col">${s.name || "-"}</td><td>${s.logged_in ? '<span class="device-yes">yes</span>' : '<span class="device-no">no</span>'}</td>`;
      for (const qid of qids) {
        const entry = s.questions[qid];
        const { cls, text } = cellHtml(entry);
        const clickable = entry ? "cell-clickable" : "";
        html += `<td class="${cls} ${clickable}" data-roll="${s.roll_no}" data-qid="${qid}">${text}</td>`;
      }
      html += `<td><strong>${s.total_earned}/${data.total_possible}</strong></td>`;
      html += `</tr>`;
    }
    html += `</tbody></table><p class="hint">* = code checks not satisfied (e.g. a required construct wasn't used), even though the tests shown passed. Total reflects live open-test runs only (last saved run per question), not the final graded result.</p>`;
    wrap.innerHTML = html;

    wrap.querySelectorAll("td.cell-clickable").forEach((td) => {
      td.addEventListener("click", () => showDetail(td.dataset.qid, td.dataset.roll));
    });
  }

  const detailModal = document.getElementById("detail-modal");
  document.getElementById("detail-modal-close").addEventListener("click", () => detailModal.close());

  async function showDetail(qid, roll) {
    const { ok, data } = await api(`/api/live/${labId}/dashboard/${encodeURIComponent(qid)}/${encodeURIComponent(roll)}/detail`);
    document.getElementById("detail-modal-title").textContent = `${roll} — ${qid}`;
    document.getElementById("detail-modal-body").innerHTML =
      ok && data.html ? data.html : '<p class="hint">No run recorded yet.</p>';
    detailModal.showModal();
  }

  /* ------------------------------------------------------------ finalize */

  let finalizePollTimer = null;

  async function pollFinalizeStatus() {
    const { data } = await api(`/api/live/${labId}/finalize/status`);
    if (data.status === "running") {
      finalizeMsgEl.textContent = "Grading in progress -- this can take a while for a full class, please wait...";
      return;
    }
    clearInterval(finalizePollTimer);
    finalizePollTimer = null;
    finalizeBtn.disabled = false;

    if (data.status === "done") {
      finalizeMsgEl.textContent = `Done. Results published as run '${data.finalized_run}'.`;
    } else if (data.status === "error") {
      finalizeMsgEl.textContent = data.error || "Finalize failed.";
    }
    if (data.stdout || data.stderr) {
      finalizeOutputEl.hidden = false;
      finalizeOutputEl.textContent = [data.stdout, data.stderr].filter(Boolean).join("\n\n");
    }
    await loadSessionStatus();
  }

  finalizeBtn.addEventListener("click", async () => {
    if (!confirm("Run the full grader now? This grades every student's saved code, including hidden tests, and publishes results/leaderboard to students.")) return;
    finalizeBtn.disabled = true;
    finalizeMsgEl.textContent = "Starting grading run...";
    finalizeOutputEl.hidden = true;
    const { ok, data } = await api(`/api/live/${labId}/finalize`, { method: "POST" });
    if (!ok) {
      finalizeBtn.disabled = false;
      finalizeMsgEl.textContent = data.error || "Failed to start grading run.";
      return;
    }
    if (finalizePollTimer) clearInterval(finalizePollTimer);
    finalizePollTimer = setInterval(pollFinalizeStatus, 3000);
    await pollFinalizeStatus();
  });

  // If the page is loaded/refreshed while a finalize job is already
  // mid-flight (e.g. the admin navigated away and back), pick up polling
  // instead of leaving the button in a stale enabled state.
  (async () => {
    const { data } = await api(`/api/live/${labId}/finalize/status`);
    if (data.status === "running") {
      finalizeBtn.disabled = true;
      finalizePollTimer = setInterval(pollFinalizeStatus, 3000);
      await pollFinalizeStatus();
    }
  })();

  document.getElementById("logout-btn").addEventListener("click", async () => {
    await api("/admin/logout", { method: "POST" });
    window.location.href = "/admin/login";
  });

  /* --------------------------------------------------------------- init */

  async function init() {
    await loadSessionStatus();
    await loadDisplayConfig();
    setInterval(loadSessionStatus, 5000);
    setInterval(() => {
      if (!document.getElementById("tab-live").hidden) loadLiveStatus();
    }, 10000);
  }

  init();
})();

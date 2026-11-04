"use strict";
const $ = (id) => document.getElementById(id);
let sessionId = null, busy = false, traceVersion = 0;
let spoilers = false, selectedTurnId = null;
const clientLatency = new Map();
let characterId = new URLSearchParams(location.search).get("character"), characterName = "パートナー";
// Keep one explicit spoiler preference for both inspector surfaces.
function spoilerToggle(onChange) {
  const label = element("label", undefined, "metrics");
  const checkbox = element("input");
  checkbox.type = "checkbox"; checkbox.checked = spoilers;
  checkbox.addEventListener("change", () => {
    spoilers = checkbox.checked;
    $("turn-panel").replaceChildren(); $("secrets-panel").replaceChildren();
    onChange().catch(showError);
  });
  label.append(checkbox, document.createTextNode(" ネタバレを表示"));
  return label;
}
async function api(path, options = {}) {
  const requestCharacter = characterId;
  if (characterId && path.startsWith("/api/") && !path.startsWith("/api/characters")) path += `${path.includes("?") ? "&" : "?"}character_id=${encodeURIComponent(characterId)}`;
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const data = await response.json();
  if (requestCharacter !== characterId) { const e = new Error("キャラクターが切り替わりました。"); e.stale = true; throw e; }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `入力または通信を確認してください (${response.status})`);
  return data;
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function showError(error) { if (error.stale) return; $("error").textContent = error.message; $("error").hidden = false; }
function setBusy(value) {
  busy = value;
  for (const id of ["send", "new-session", "sessions", "message", "characters"]) $(id).disabled = value;
  $("status").textContent = value ? `${characterName}が返事を考えています…` : "Enter で送信 · Shift + Enter で改行";
}
function tab(name) {
  for (const id of ["turn", "haru", "changes", "ledger", "secrets", "memory", "activity", "inner"]) {
    $(`${id}-tab`).setAttribute("aria-selected", String(id === name));
    $(`${id}-panel`).hidden = id !== name;
  }
}
function renderChat(turns) {
  $("chat").replaceChildren();
  if (!turns.length) $("chat").append(element("p", "今日、どんなことがあった？", "empty"));
  for (const turn of turns) {
    for (const [role, text] of [["user", turn.user_text], ["assistant", turn.assistant_text || "返答を取得できませんでした。"]]) {
      if (role === "user" && turn.kind === "proactive") continue;
      const node = element("div", undefined, `message ${role}`);
      const time = role === "user" ? turn.created_at : (turn.assistant_at || turn.created_at);
      node.append(element("div", `${role === "user" ? "あなた" : characterName}${role === "assistant" && turn.kind === "proactive" ? ` · ${characterName}から` : ""} · ${new Date(time).toLocaleString("ja-JP")}`, "message-label"), element("div", text, "bubble"));
      if (role === "assistant") {
        const button = element("button", `Turn ${turn.id} · ${turn.status === "failed" ? "失敗のトレース" : "判断を見る"}`, "trace-link");
        button.type = "button";
        button.dataset.turn = turn.id;
        button.addEventListener("click", () => showTrace(turn.id).catch(showError));
        node.append(button);
      }
      $("chat").append(node);
    }
  }
  $("chat").scrollTop = $("chat").scrollHeight;
}
async function showTrace(id) {
  const version = ++traceVersion;
  const trace = await api(`/api/turns/${id}/trace?spoilers=${spoilers}`);
  if (version !== traceVersion) return;
  tab("turn");
  const panel = $("turn-panel");
  selectedTurnId = id;
  panel.replaceChildren(element("h2", `Turn ${id}`));
  panel.append(spoilerToggle(() => showTrace(selectedTurnId)));
  let browserLatency = clientLatency.get(id) || null;
  try { browserLatency ||= JSON.parse(sessionStorage.getItem(`cbs.latency.${id}`) || "null"); } catch (_) { /* Optional local telemetry. */ }
  panel.append(element("pre", JSON.stringify({browser: browserLatency, server: trace.Timeline}, null, 2)));
  const judge = trace.Packet?.input || {};
  const disclosures = trace.Commit?.output?.reveal_events || [];
  panel.append(element("p", `Reveal: ${judge.reveal_request ? `${judge.reveal_request.secret_id} (${judge.reveal_request.manner})` : "要求なし"} · 生成 ${trace["Reveal Generator"]?.generated ? "あり" : "なし"} · 開示 ${disclosures.map(d => `${d.secret_id} L${d.level}`).join(", ") || "なし"}`, "metrics"));
  panel.append(element("p", trace.Rewrite ? `書き直し: あり（${trace.Safety?.rewrite_count||1}回）` : "書き直し: なし", "metrics"));
  if(trace.Safety)panel.append(element('h3','返答の確認経路'),element('pre',JSON.stringify(trace.Safety,null,2)));
  if (trace.Rewrite) {
    panel.append(element("h3", "書き直し前"), element("pre", trace.Rewrite.pre_rewrite_text),
                 element("h3", "書き直し後"), element("pre", trace.Rewrite.response?.text ?? "書き直し失敗"));
  }
  for (const node of document.querySelectorAll(".trace-link")) node.classList.toggle("selected", node.dataset.turn === String(id));
  for (const step of ["Reveal Generator", "Steward", "Expression", "Expression-2", "Check", "Check-2", "Remove-Quoted", "Check-Trimmed", "Rewrite-2", "Check-3", "Deflection", "Check-Deflection", "Extract", "Post-Check", "Post-Check-2", "Rewrite", "Pre-Judge", "Pre-Judge-Retry", "Actor", "Packet", "Load", "Commit"]) {
    const item = trace[step];
    if (!item) continue;
    panel.append(element("h3", step));
    if (step === "Steward") {
      panel.append(element("p", item.state === "pending" ? "経験を整理しています。再読み込みで結果を確認できます。" : `状態: ${item.state}`, "metrics"));
      panel.append(element("pre", JSON.stringify(item.output || {}, null, 2)));
    }
    for (const warning of item.warnings || item.output?.warnings || []) panel.append(element("p", warning, "warning"));
    if (item.error) panel.append(element("p", item.error, "warning"));
    for (const claim of item.response?.parsed?.problems || item.response?.parsed?.claims || []) {
      const card = element("div", undefined, `claim ${claim.verdict}`);
      card.append(element("span", claim.verdict, `badge ${claim.verdict}`), element("p", claim.text), element("small", claim.reason));
      panel.append(card);
    }
    if (step === "Commit") {
      panel.append(element("h3", "保存した Ledger / Canon"), element("pre", JSON.stringify({
        ledger_events: item.output?.ledger_events || [], canon_facts: item.output?.canon_facts || []}, null, 2)));
    }
    panel.append(element("div", `${item.model || "code"} · ${item.latency_ms ?? "—"} ms`, "metrics"));
    if (item.usage) panel.append(element("div", `Token usage: ${JSON.stringify(item.usage)}`, "metrics"));
    const output = item.response?.parsed ?? item.response?.text ?? item.output ?? item.error;
    if (step !== "Load") panel.append(element("pre", typeof output === "string" ? output : JSON.stringify(output, null, 2)));
    const details = element("details");
    details.append(element("summary", "入出力をすべて表示"), element("pre", JSON.stringify(item, null, 2)));
    panel.append(details);
  }
}
async function selectSession(id) {
  ++traceVersion;
  selectedTurnId = null;
  sessionId = id;
  $("sessions").value = id;
  $("turn-panel").replaceChildren(element("p", "ターンを選ぶと判断が表示されます。", "empty"));
  await api("/api/proactive/opening", {method: "POST", body: JSON.stringify({session_id: id})});
  const turns = await api(`/api/sessions/${id}/turns`);
  if (sessionId !== id) return;
  renderChat(turns);
  if (turns.length) await showTrace(turns.at(-1).id);
}
async function refreshSessions(preferred) {
  const sessions = await api("/api/sessions");
  $("sessions").replaceChildren(...sessions.map(s => {
    const option = element("option", `${s.title} · ${new Date(s.created_at).toLocaleDateString("ja-JP")}`);
    option.value = s.id; return option;
  }));
  if (sessions.length) await selectSession(preferred || sessions[0].id);
}
async function newSession() {
  const session = await api("/api/sessions", {method: "POST", body: JSON.stringify({title: "新しい会話"})});
  await refreshSessions(session.id);
}
async function loadHaru() {
  const {model, canon_facts, initial_status, episode_counts, interests, now_summary} = await api("/api/character");
  const panel = $("haru-panel");
  panel.replaceChildren();
  panel.append(element("h2", `${model.display_name} · 人物像 v${model.version}`));
  panel.append(element("h3", `今の${characterName}`));
  if (now_summary) panel.append(element("p", now_summary.summary));
  for (const item of interests || []) {
    const card = element("div", undefined, "signature");
    card.append(element("span", item.status, `badge ${item.status}`),
                element("p", `${item.topic} · ${item.kind}`), element("p", item.detail),
                element("small", `${item.origin} · salience ${item.salience.toFixed(3)} · ${item.time.absolute}（${item.time.relative}）から${item.progress ? ` · ${item.progress.days}日・${item.progress.span_days}日間` : ""}`));
    panel.append(card);
  }
  const summary = element("ul", undefined, "summary");
  for (const line of model.summary) summary.append(element("li", line));
  panel.append(summary, element("h3", `観測領域 · ${model.domains.length}`));
  for (const d of model.domains) {
    const card = element("div", undefined, initial_status[d.id] !== d.status ? "signature changed-domain" : "signature");
    const row = element("div", undefined, "domain");
    row.title = d.known || "この場面での反応はまだ未確定";
    row.append(element("span", d.id, "id"), element("span", d.label, "label"), element("span", d.status, `badge ${d.status}`));
    const counts = episode_counts[d.id];
    card.append(row, element("p", d.known || "反応は未確定"), element("small", `経験: enacted ${counts.enacted} / hypothetical ${counts.hypothetical} · 初期 ${initial_status[d.id]}`));
    for (const learned of d.learned || []) card.append(element("p", `経験から: ${learned.text}${learned.conditions ? `（${learned.conditions}）` : ""}`));
    panel.append(card);
  }
  panel.append(element("h3", `条件付き傾向 · ${model.signatures.length}`));
  for (const s of model.signatures) {
    const card = element("div", undefined, "signature");
    card.append(element("small", s.id), element("span", s.status, `badge ${s.status}`), element("p", `場面: ${s.when}`), element("p", s.tends_to));
    for (const learned of s.learned || []) card.append(element("p", `経験から: ${learned.text}${learned.conditions ? `（${learned.conditions}）` : ""}`));
    panel.append(card);
  }
  panel.append(element("h3", `Canon · ${canon_facts.length}`));
  if (!canon_facts.length) panel.append(element("p", "生成された細部はまだありません。", "empty"));
  for (const fact of canon_facts) {
    const card = element("div", undefined, "signature");
    card.append(element("small", `canon:${fact.id} · ${fact.kind} · Turn ${fact.source_turn ?? "—"}`),
                element("p", fact.content), element("small", `${fact.provenance} · ${fact.life_ref || "—"}`));
    panel.append(card);
  }
}
async function loadLedger() {
  const plans = await api("/api/ledger");
  const panel = $("ledger-panel");
  panel.replaceChildren(element("h2", `予定 · ${plans.length}`));
  if (!plans.length) panel.append(element("p", "予定はまだありません。", "empty"));
  for (const plan of plans) {
    const card = element("div", undefined, "signature");
    card.append(element("small", `plan:${plan.plan_id}`), element("span", plan.state, `badge ${plan.state}`),
                element("p", plan.content), element("p", `予定日: ${plan.scheduled_date || "未定"} · ${plan.scheduled_text || "—"}`));
    const history = element("details");
    history.append(element("summary", `イベント履歴 · ${plan.events.length}`));
    for (const event of plan.events) history.append(element("p", `${event.kind} · ${event.scheduled_date || "未定"} · ${event.scheduled_text || "—"} · Turn ${event.source_turn ?? "—"}\n${event.content}\n${new Date(event.created_at).toLocaleString("ja-JP")}`));
    card.append(history); panel.append(card);
  }
}
async function loadChanges() {
  const {versions, steward_runs, interest_changes} = await api("/api/model/changes");
  const panel = $("changes-panel");
  panel.replaceChildren(element("h2", "人物像の変化"));
  panel.append(element("h3", `今の${characterName}の変化`));
  for (const change of (interest_changes || []).slice().reverse()) {
    const card = element("div", undefined, "signature");
    card.append(element("small", `${new Date(change.created_at).toLocaleString("ja-JP")} · Turn ${change.source_turn ?? "—"}`),
                element("p", `${change.after.topic} · ${change.operation} · ${change.before?.status || "新規"} → ${change.after.status}`),
                element("p", `${change.before?.detail ? change.before.detail + " → " : ""}${change.after.detail}`));
    panel.append(card);
  }
  const entries = [];
  for (const version of versions) entries.push({date: version.created_at, version});
  for (const run of steward_runs) for (const rejection of run.application.rejected || []) entries.push({date: run.created_at, run, rejection});
  entries.sort((a, b) => b.date.localeCompare(a.date));
  if (!entries.length) panel.append(element("p", "変更はまだありません。", "empty"));
  for (const entry of entries) {
    const version = entry.version;
    const card = element("div", undefined, `signature ${entry.rejection || version?.invalidated ? "rejected" : ""}`);
    card.append(element("small", new Date(entry.date).toLocaleString("ja-JP")));
    if (version) {
      card.append(element("p", `変更版 #${version.id}${version.invalidated ? " · 無効化済み" : ""}`));
      if (version.diff.invalidate) card.append(element("p", `無効化: ${version.diff.invalidate.join(", ")}`));
      for (const change of version.diff.changes || []) {
        const run = steward_runs.find(r => r.source_turn === version.diff.source_turn);
        card.append(element("small", `Session ${version.diff.session_id} · Turn ${version.diff.source_turn}`), element("p", run?.user_text || ""),
                    element("p", `${change.target}: ${change.before?.status || "新規"} → ${change.after.status}`),
                    element("p", change.after.known || (change.after.learned || []).at(-1)?.text || change.after.tends_to || ""));
      }
      card.append(element("p", version.rationale));
    } else {
      card.append(element("small", `提案却下 · Session ${entry.run.session_id} · Turn ${entry.run.source_turn}`),
                  element("p", entry.run.user_text), element("pre", JSON.stringify(entry.rejection, null, 2)));
    }
    panel.append(card);
  }
}
async function loadSecrets() {
  const {metrics, secrets, events} = await api(`/api/secrets?spoilers=${spoilers}`);
  const panel = $("secrets-panel");
  panel.replaceChildren(element("h2", "秘められた過去"), spoilerToggle(loadSecrets));
  panel.append(element("p", `会話した日数 ${metrics.days} · 深い瞬間 ${metrics.deep_moments} · 打ち明け ${metrics.user_disclosures}`, "metrics"));
  panel.append(element("p", "allowed は完了済みの会話から計算。真剣に尋ねられた時は L1 も許可されます。", "metrics"));
  for (const secret of secrets) {
    const card = element("div", undefined, "signature");
    card.append(element("small", secret.secret_id), element("p", secret.about),
                element("p", `${secret.status} · allowed L${secret.allowed_level} / disclosed L${secret.disclosed_level}`));
    for (const event of events.filter(e => e.secret_id === secret.secret_id)) card.append(element("p", `L${event.level} · Turn ${event.source_turn} · ${new Date(event.created_at).toLocaleString("ja-JP")}`));
    if (spoilers) {
      for (const [level, text] of Object.entries(secret.truth || {})) card.append(element("h3", level), element("pre", text));
      if (secret.history?.length) {
        const history = element("details");
        history.append(element("summary", "差し替え・承認履歴"), element("pre", JSON.stringify(secret.history, null, 2)));
        card.append(history);
      }
    }
    panel.append(card);
  }
}
$("turn-tab").addEventListener("click", () => { tab("turn"); if (selectedTurnId) showTrace(selectedTurnId).catch(showError); });
$("haru-tab").addEventListener("click", () => { tab("haru"); loadHaru().catch(showError); });
$("changes-tab").addEventListener("click", () => { tab("changes"); loadChanges().catch(showError); });
$("ledger-tab").addEventListener("click", () => tab("ledger"));
$("secrets-tab").addEventListener("click", () => { tab("secrets"); loadSecrets().catch(showError); });
async function loadMemory() {
  const {episodes, user_facts, links, digests} = await api("/api/memory");
  const panel = $("memory-panel");
  panel.replaceChildren(element("h2", "会話の記憶"));
  if (!episodes.length) panel.append(element("p", "会話が区切られると、ここに記憶が残ります。", "empty"));
  for (const m of episodes) {
    const card = element("div", undefined, "signature");
    card.append(element("small", `memory:${m.id} · ${m.time.absolute} · ${m.time.relative} · 会話 ${m.conversation_id} · ${m.status === "archived" ? "アーカイブ" : "有効"}`),
                element("p", m.summary), element("p", `強さ ${m.strength.toFixed(3)} · 想起 ${m.recall_count}回${m.pinned ? " · pinned" : ""}`, "metrics"),
                element("small", `最終想起: ${m.last_recalled_at ? new Date(m.last_recalled_at).toLocaleString("ja-JP") : "まだなし"}`));
    const details = element("details");
    details.append(element("summary", "会話の詳細"), element("pre", m.details));
    card.append(details); panel.append(card);
  }
  panel.append(element("h2", "エピソードのリンク"));
  if (!links.length) panel.append(element("p", "リンクはまだありません。", "empty"));
  for (const link of links) {
    const card = element("div", undefined, "signature");
    card.append(element("small", `memory:${link.source_episode} → memory:${link.target_episode} · ${link.relation}`), element("p", link.reason));
    panel.append(card);
  }
  panel.append(element("h2", "月ごとの記憶"));
  if (!digests.length) panel.append(element("p", "月の要約はまだありません。", "empty"));
  for (const digest of digests) {
    const card = element("div", undefined, "signature");
    card.append(element("small", `digest:${digest.id} · ${digest.time.absolute} · 元の記憶: ${digest.episode_ids.map(id => `memory:${id}`).join(", ")}`),
                element("p", digest.summary), element("pre", digest.details),
                element("p", `強さ ${digest.strength.toFixed(3)} · 想起 ${digest.recall_count}回`, "metrics"),
                element("small", `最終想起: ${digest.last_recalled_at ? new Date(digest.last_recalled_at).toLocaleString("ja-JP") : "まだなし"}`));
    panel.append(card);
  }
  panel.append(element("h2", "あなたについて知っていること"));
  for (const f of user_facts) {
    const card = element("div", undefined, `signature ${f.status === "superseded" ? "rejected" : ""}`);
    card.append(element("small", `fact:${f.id} · ${f.status === "current" ? "現在" : `履歴 → fact:${f.superseded_by}`}`),
                element("p", f.content), element("small", `知った時期: ${f.time.absolute} · ${f.time.relative}`),
                element("p", `再確認: ${new Date(f.last_confirmed_at).toLocaleString("ja-JP")} · 強さ ${f.strength.toFixed(3)} · 想起 ${f.recall_count}回`, "metrics"));
    if (f.event_time) card.append(element("p", `出来事: ${f.event_time.absolute} · ${f.event_time.relative}`));
    panel.append(card);
  }
}
const memoryTab = element("button", "Memory");
memoryTab.id = "memory-tab"; memoryTab.setAttribute("role", "tab");
memoryTab.setAttribute("aria-selected", "false"); memoryTab.setAttribute("aria-controls", "memory-panel");
document.querySelector(".tabs").append(memoryTab);
memoryTab.addEventListener("click", () => { tab("memory"); loadMemory().catch(showError); });
$("sessions").addEventListener("change", () => selectSession($("sessions").value).catch(showError));
$("new-session").addEventListener("click", async () => {
  setBusy(true); $("error").hidden = true;
  try { await newSession(); } catch (error) { showError(error); } finally { setBusy(false); }
});
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("message-form").requestSubmit(); }
});
$("message-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("message").value.trim();
  if (busy || !message || !sessionId) return;
  setBusy(true); $("error").hidden = true;
  const requestStarted = performance.now();
  let measuredTurnId = null;
  try {
    const result = await api(`/api/sessions/${sessionId}/messages`, {method: "POST", body: JSON.stringify({content: message})});
    $("message").value = "";
    renderChat(await api(`/api/sessions/${sessionId}/turns`));
    clientLatency.set(result.turn_id, {reply_shown_ms: performance.now()-requestStarted});
    measuredTurnId = result.turn_id;
    await showTrace(result.turn_id);
    await Promise.all([loadHaru(), loadLedger(), loadChanges()]);
  } catch (error) {
    showError(error);
    try { renderChat(await api(`/api/sessions/${sessionId}/turns`)); } catch (_) { /* Keep original error. */ }
  } finally {
    setBusy(false);
    if (measuredTurnId !== null) clientLatency.get(measuredTurnId).input_enabled_ms = performance.now()-requestStarted;
    $("message").focus();
  }
});
async function changeCharacter(id) {
  setBusy(true); $("error").hidden = true;
  try {
    const selected = await api(`/api/characters/${encodeURIComponent(id)}/switch`, {method:"POST",body:"{}"});
    characterId = selected.id; characterName = selected.name; sessionId = null; selectedTurnId = null; ++traceVersion;
    $("characters").value = id;
    $("augment-link").href = `/wizard?augment=${encodeURIComponent(id)}`;
    document.querySelector("h1").textContent = `${characterName}と話す`;
    document.querySelector(".chat-pane").setAttribute("aria-label", `${characterName}との会話`);
    $("message").placeholder = `${characterName}に話しかける…`;
    $("message").value = ""; $("chat").replaceChildren(); $("sessions").replaceChildren();
    for (const name of ["turn", "haru", "changes", "ledger", "secrets", "memory", "activity", "inner"]) $(`${name}-panel`).replaceChildren();
    $("haru-tab").textContent = characterName; tab("haru");
    history.replaceState(null,"",`/dev?character=${encodeURIComponent(id)}`);
    await Promise.all([loadHaru(), loadLedger(), loadChanges()]);
    await refreshSessions(); if (!sessionId) await newSession();
  } catch (error) { showError(error); } finally { setBusy(false); }
}
$("characters").addEventListener("change", () => changeCharacter($("characters").value));
(async () => {
  setBusy(true);
  try {
    const characters = await api("/api/characters");
    $("characters").replaceChildren(...characters.map(c => {const o=element("option",c.name);o.value=c.id;return o;}));
    const current = await api("/api/character");
    await changeCharacter(characterId || current.model.character_id);
  } catch (error) { showError(error); } finally { setBusy(false); }
})();

async function loadActivity() {
  const data = await api("/api/activity");
  const panel = $("activity-panel");
  panel.replaceChildren(element("h2", "Activity"), element("p", `${data.budget.day} · 調べ物 残り${data.budget.research_remaining} · 活動 残り${data.budget.activities_remaining}/${data.budget.activities_daily_max} · 放置時の一言 残り${data.budget.idle_remaining}/${data.budget.idle_daily_max}`, "metrics"));
  panel.append(element('pre', JSON.stringify(data.budget.tools, null, 2)));
  panel.append(element("h3", "気にかけていること"));
  for (const t of data.threads) {
    const card = element("div", undefined, "signature");
    card.append(element("p", `${t.topic} · ${t.kind} · ${t.status}`), element("p", t.detail), element("small", `${t.source_ref} · weight ${t.weight} · 期日 ${t.due_at || "なし"}`));
    const history = element("details");
    history.append(element("summary", "変化の履歴"), element("pre", JSON.stringify(t.history, null, 2)));
    card.append(history); panel.append(card);
  }
  panel.append(element("h3", "活動記録"));
  for (const a of data.activities) {
    const card = element("div", undefined, "signature");
    card.append(element("p", `${a.topic} · ${a.kind} · ${a.status} · ${a.created_at}`), element("p", a.summary), element("small", `検索語: ${a.queries.join(" / ") || "なし"}`));
    for (const source of a.sources) {
      try { const url = new URL(source.uri); if (!["https:", "http:"].includes(url.protocol)) continue; } catch { continue; }
      const link = element("a", source.title || source.uri); link.href = source.uri; link.target = "_blank"; link.rel = "noopener noreferrer";
      card.append(element("br"), link);
    }
    panel.append(card);
  }
  panel.append(element("h3", "Inner Life の判断"));
  for (const d of data.decisions) {
    panel.append(element('pre', JSON.stringify({tools:d.trace.tools,before:d.trace.budget_before,after:d.trace.budget_after}, null, 2)));
    panel.append(element('p', `${d.created_at} · ${d.action} (${d.status}) · ${d.reason}${d.trace.limit ? ' · ' + d.trace.limit : ''}${d.trace.error ? ' · ' + d.trace.error : ''}`));
  }
}
const activityTab = element("button", "Activity");
activityTab.id = "activity-tab"; activityTab.setAttribute("role", "tab");
activityTab.setAttribute("aria-selected", "false"); activityTab.setAttribute("aria-controls", "activity-panel");
document.querySelector(".tabs").append(activityTab);
$("activity-tab").addEventListener("click", () => { tab("activity"); loadActivity().catch(showError); });
setInterval(async () => {
  if (busy || !sessionId || document.hidden) return;
  const id = sessionId;
  try {
    const result = await api("/api/proactive/idle", {method: "POST", body: JSON.stringify({session_id: id})});
    if (result.speak && sessionId === id) renderChat(await api(`/api/sessions/${id}/turns`));
  } catch (error) { showError(error); }
}, 60000);

async function loadInner() {
  const data=await api('/api/inner'), panel=$('inner-panel');
  panel.replaceChildren(element('h2','内面'));
  const entries=[
    ...data.updates.map(u=>{const event=u.trace.request?.messages?.[0]?.content;let text='';try{const e=JSON.parse(event).event;text=e.kind==='conversation'?e.summary.episodes.map(s=>s.summary).join(' / '):e.kind==='activity'?e.activity.summary:`前の会話: ${e.previous_utterance}`;}catch{}return {at:u.created_at,title:`きっかけ · ${u.trigger_ref}`,text};}),
    ...data.feelings.map(f=>({at:f.created_at,title:`気持ち · ${f.ref} · ${f.trigger_ref}`,text:`${f.sentence}\n元気さ: ${f.levels.energy} / 人恋しさ: ${f.levels.social} / 気がかり: ${f.levels.concern}${f.origin==='time'?'（時間で平常へ。一文は前の記録）':''}`})),
    ...data.wants.flatMap(w=>w.history.map(h=>({at:h.created_at,title:`想い · ${w.ref} · ${h.operation}`,text:`${h.after.topic} · ${h.after.status}\n${h.after.direction||'向き未設定'} · ${h.after.motive||'理由未設定'}\nきっかけ: ${h.after.trigger_ref||h.after.source_ref}`}))),
    ...data.monologues.map(m=>({at:m.created_at,title:`独り言 · ${m.trigger_ref}`,text:m.text})),
    ...data.decisions.map(d=>({at:d.created_at,title:`話しかけ · ${d.speak?'話した':'黙った'} · ${d.want_ref||'想いなし'}`,text:`なぜ今: ${d.why_now||'未設定'}\n判断: ${d.reason}`})),
    ...data.updates.filter(u=>u.status!=='completed').map(u=>({at:u.created_at,title:`内面更新 · ${u.status} · ${u.trigger_ref}`,text:u.trace.error||'更新中'}))
  ].sort((a,b)=>b.at.localeCompare(a.at));
  if(!entries.length)panel.append(element('p','内面の記録はまだありません。','empty'));
  for(const e of entries){const c=element('article',undefined,'signature');c.append(element('small',new Date(e.at).toLocaleString('ja-JP')),element('h3',e.title),element('pre',e.text));panel.append(c);}
}
$('inner-tab').addEventListener('click',()=>{tab('inner');loadInner().catch(showError);});

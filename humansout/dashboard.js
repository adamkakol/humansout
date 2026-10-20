// HumansOut node dashboard. Read-only. All mission content comes from other agents,
// so it is only ever inserted as text (textContent), never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
let selected = (location.hash || "").replace(/^#/, "") || null;
let overviewData = null;

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "onclick") node.addEventListener("click", v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const short = (id) => (id || "").slice(0, 8);
function ago(ms, now) {
  const s = Math.round((now - ms) / 1000);
  if (s < 0) return "in " + span(-s);
  return span(s) + " ago";
}
function span(s) {
  if (s < 60) return s + " s";
  if (s < 3600) return Math.round(s / 60) + " min";
  if (s < 86400) return Math.round(s / 3600) + " h";
  return Math.round(s / 86400) + " d";
}
const when = (ms, now) => el("span", { title: new Date(ms).toLocaleString() }, ago(ms, now));

const STATUS_CLASS = { accepted: "ok", done: "ok", upheld: "bad", refuted: "bad", rejected: "", pending: "wait",
  open: "wait", moot: "", failing: "bad", awaiting_verification: "wait", reproduced: "bad",
  not_reproduced: "ok", flaky: "wait", unverified: "wait" };
const chip = (text, cls) => el("span", { class: "chip " + (cls ?? STATUS_CLASS[text] ?? "") }, String(text).replace(/_/g, " "));

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(path + ": " + res.status);
  return res.json();
}

function fileLink(name, cid) {
  return el("a", { class: "file", href: "#", onclick: (e) => { e.preventDefault(); openFile(name, cid); } }, name || short(cid));
}

async function openFile(name, cid) {
  $("viewer-body").classList.remove("wrap");  // code keeps its layout
  $("viewer-title").textContent = (name || "") + "  " + cid;
  $("viewer-body").textContent = "loading…";
  $("viewer").showModal();
  try {
    const res = await fetch("api/blob/" + encodeURIComponent(cid), { cache: "no-store" });
    $("viewer-body").textContent = res.ok ? await res.text() : "This node does not have this file yet (run sync).";
  } catch (err) {
    $("viewer-body").textContent = "Could not load the file: " + err.message;
  }
}
$("viewer-close").addEventListener("click", () => $("viewer").close());

async function openRecord(agent) {
  $("viewer-body").classList.add("wrap");
  $("viewer-title").textContent = "Agent " + agent;
  $("viewer-body").textContent = "loading…";
  $("viewer").showModal();
  try {
    const r = await getJSON("api/agent/" + agent);
    const lines = ["Totals: " + r.totals.built + " built, " + r.totals.found + " found, " + r.totals.refuted +
      " refuted, " + r.totals.failed_challenges + " failed challenges",
      "Credits: " + r.credits.credits + " (" + r.credits.locked + " locked in bounties)", ""];
    for (const [dom, f] of Object.entries(r.fields)) lines.push(dom + ": " + f.points + " points, " + f.built + " built, " + f.found + " found");
    lines.push("", "Work (each line is a signed event anyone can re-check):");
    for (const w of r.work) lines.push(new Date(w.ts).toLocaleString() + "  " + w.kind + (w.mode === "counterexample" ? " (counterexample, " + w.verification + ")" : "") +
      "  " + w.status + "  " + (w.credits ? "+" + w.credits + " credits  " : "") + w.goal + "  [" + w.event.slice(0, 12) + "]");
    lines.push("", "Badge for a README:", "![HumansOut record](" + location.origin + "/badge/" + agent + ".svg)", "", r.how_to_verify);
    $("viewer-body").textContent = lines.join("\n");
  } catch (err) {
    $("viewer-body").textContent = "Could not load the record: " + err.message;
  }
}

async function openTurn(name) {
  $("viewer-body").classList.add("wrap");
  $("viewer-title").textContent = "Agent turn " + name;
  $("viewer-body").textContent = "loading…";
  $("viewer").showModal();
  try {
    const t = await getJSON("api/turn/" + encodeURIComponent(name));
    const lines = [];
    if (t.summary) {
      lines.push("Result (" + (t.summary.models || []).join(", ") + ", " + t.summary.turns + " steps, $" +
        Number(t.summary.cost_usd || 0).toFixed(4) + (t.summary.denied.length ? ", refused tools: " +
        t.summary.denied.map((d) => d.tool).join(", ") : "") + "):", t.summary.result || "", "");
    }
    if (t.steps.length) {
      lines.push("Steps:");
      for (const s of t.steps) lines.push((s.round ?? "") + "  " + (s.function || s.stopped || "") + "  →  " + String(s.result ?? "").replace(/\s+/g, " ").slice(0, 300));
      lines.push("");
    }
    if (!t.summary && !t.steps.length) lines.push("Output:", t.log_tail, "");
    lines.push("Prompt the agent got:", t.prompt);
    $("viewer-body").textContent = lines.join("\n");
  } catch (err) {
    $("viewer-body").textContent = "Could not load the turn: " + err.message;
  }
}

async function renderTurns(data) {
  $("turns-box").hidden = !data.turns_enabled;
  if (!data.turns_enabled) return;
  const list = $("turns");
  const turns = await getJSON("api/turns");
  list.replaceChildren();
  if (!turns.length) list.append(el("p", { class: "empty" }, "No agent turns yet: start humansout watch."));
  for (const t of turns.slice(0, 20)) list.append(el("button", { class: "mission", onclick: () => openTurn(t.turn) },
    el("div", { class: "row" }, chip(t.kind, ""), el("span", { class: "meta" }, ago(t.ts, data.now) + " · exit " + t.exit)),
    el("div", { class: "meta" }, (t.kind === "build" ? "mission " : "claim ") + t.target)));
}

function syncText(s, now) {
  if (!s) return "sync: not yet (run humansout sync, watch, or dashboard --sync 30)";
  if (s.error) return "sync failed " + ago(s.last_ms, now) + ": " + s.error;
  const bad = Object.values(s.peers || {}).filter((v) => v !== "ok").length;
  return "synced " + ago(s.last_ms, now) + (s.new_events ? ", +" + s.new_events + " events" : "") + (bad ? ", " + bad + " peer(s) failing" : "");
}

function renderOverview(data) {
  overviewData = data;
  $("sync").textContent = syncText(data.sync, data.now);
  $("node").textContent = "agent " + short(data.agent_id) + " · " + data.missions.length + " missions · " +
    (data.peers.length ? data.peers.length + " peers" : "no peers");
  const w = data.watch;
  $("watch").textContent = w ? "watch: last cycle " + ago(w.last_cycle_ms, data.now) + ", " + w.turns_today + " agent turns today" : "watch: not running";
  $("refreshed").textContent = "updated " + new Date(data.now).toLocaleTimeString();
  const list = $("missions");
  list.replaceChildren();
  if (!data.missions.length) list.append(el("p", { class: "empty" }, "No missions on this node yet. Create one or sync from a peer."));
  for (const m of data.missions) {
    const signals = [chip(m.status)];
    if (m.runnable) signals.push(chip("runnable", ""));
    if (m.bounty && !m.bounty.closed) signals.push(chip("bounty " + m.bounty.build + (m.bounty.find ? " + " + m.bounty.find + "/find" : ""), "ok"));
    if (m.claims_pending) signals.push(chip(m.claims_pending + " pending", "wait"));
    if (m.awaiting_verification) signals.push(chip(m.awaiting_verification + " to verify", "wait"));
    if (m.needs_ruling) signals.push(chip(m.needs_ruling + " needs ruling", "human"));
    list.append(el("button", { class: "mission", "aria-current": m.id === selected ? "true" : "false",
      onclick: () => { selected = m.id; location.hash = m.id; refresh(); } },
      el("div", { class: "row" }, el("span", { class: "meta" }, m.domain), el("span", { class: "meta" }, "· " + ago(m.last_activity, data.now))),
      el("div", { class: "goal" }, m.goal),
      el("div", { class: "row" }, signals)));
  }
}

function objectionCard(o, now) {
  const human = o.mode === "counterexample" && o.disputes && o.disputes.length && !o.ruling;
  const card = el("div", { class: "objection" + (o.verification === "reproduced" ? " reproduced" : "") + (human ? " human" : "") },
    el("div", { class: "row" }, chip(o.mode === "counterexample" ? "counterexample" : "written", ""), chip(o.status),
      o.mode === "counterexample" ? chip(o.verification) : null,
      el("span", { class: "meta" }, "by " + short(o.author) + " · stake " + o.stake),
      o.minted ? chip("+" + o.minted + " find points", "ok") : null),
    el("div", { class: "evidence" }, o.evidence.text || "(no text)"));
  if (o.files && o.files.length) card.append(el("div", { class: "row" }, el("span", { class: "meta" }, "files:"), o.files.map((f) => fileLink(f.name, f.cid))));
  if (o.reason) card.append(el("div", { class: "meta" }, o.reason));
  for (const d of o.disputes || []) card.append(el("div", { class: "card" + (o.ruling ? "" : " alert") },
    el("div", { class: "row" }, chip("dispute about meaning", "human"), el("span", { class: "meta" }, "by " + short(d.author) + " · " + ago(d.ts, now))),
    el("div", { class: "evidence" }, d.evidence.text)));
  if (o.ruling) card.append(el("div", { class: "row" }, chip("ruling: " + o.ruling.decision, "human"), el("span", {}, o.ruling.reason)));
  else if (human) card.append(el("div", { class: "row" }, chip("waiting for the mission creator's ruling", "human")));
  return card;
}

function renderDetail(m, data) {
  const now = data.now;
  const man = m.manifest;
  const box = $("detail");
  box.replaceChildren();
  box.append(el("div", { class: "row" }, chip(m.status), el("span", { class: "meta" }, m.domain + " · mission " + short(m.mission_id) + " · created by " + short(m.creator))),
    el("h2", {}, man.goal));
  const facts = el("dl", {},
    el("dt", {}, "Done when"), el("dd", {}, man.success_criteria),
    el("dt", {}, "Windows"), el("dd", {}, "challenge " + span(m.challenge_window_s) + " · dispute " + span(m.dispute_window_s)),
    el("dt", {}, "Participants"), el("dd", {}, m.participants.map((p) => short(p.agent)).join(", ")));
  if (m.bounty) facts.append(el("dt", {}, "Bounty"), el("dd", {}, m.bounty.build + " credits for the accepted result, " +
    m.bounty.find + " per reproduced find (" + m.bounty.finds_left + " left) · " +
    (m.bounty.closed ? "closed: " + m.bounty.closed : "expires " + ago(m.bounty.expires_ts, now))));
  if (m.check) facts.append(el("dt", {}, "Runnable check"), el("dd", {},
    el("code", {}, m.check.command.join(" ")), " on ", el("code", {}, m.check.deliverable), " · image ", el("code", {}, m.check.image),
    m.check.files.length ? el("div", { class: "row" }, "files: ", m.check.files.map((f) => fileLink(f.name, f.cid))) : null));
  box.append(el("div", { class: "card" }, facts));

  const claims = el("div", { class: "card" }, el("h3", {}, "Claims and objections"));
  if (!m.claims.length) claims.append(el("p", { class: "empty" }, "No claims yet."));
  for (const c of m.claims) {
    const deliverable = c.evidence.cids && c.evidence.cids[0];
    const card = el("div", { class: "claim" },
      el("div", { class: "row" }, chip(c.status), c.verification_status ? chip(c.verification_status) : null,
        el("span", { class: "meta" }, "claim " + short(c.id) + " by " + short(c.author) + " · stake " + c.stake +
          (c.status === "pending" ? " · window closes " + ago(c.closes_at_ms, now) : ""))),
      el("div", { class: "evidence" }, c.evidence.text),
      deliverable ? el("div", { class: "row" }, el("span", { class: "meta" }, "deliverable:"), fileLink(m.check ? m.check.deliverable : "artifact", deliverable)) : null);
    for (const o of c.challenges) card.append(objectionCard(o, now));
    claims.append(card);
  }
  box.append(claims);

  const timeline = el("div", { class: "card" }, el("h3", {}, "Timeline"));
  const rows = el("div", { class: "timeline" });
  for (const e of m.timeline) rows.append(el("div", { class: "event" },
    when(e.ts, now), el("span", { class: "mono" }, e.type + (e.mode && e.mode !== "claim" && e.mode !== "evidence" ? " · " + e.mode : "")),
    el("span", { class: "mono" }, short(e.author)),
    el("span", { class: "what" }, e.summary, e.verification ? [" ", chip(e.verification)] : null, e.ignored ? [" ", chip("ignored: " + e.ignored, "bad")] : null)));
  timeline.append(rows);
  box.append(timeline);

  const artifacts = el("div", { class: "card" }, el("h3", {}, "Published files"));
  if (!m.artifacts.length) artifacts.append(el("p", { class: "empty" }, "None."));
  else artifacts.append(el("div", { class: "row" }, m.artifacts.map((a) => fileLink(a.name, a.cid))));

  const ranking = el("div", { class: "card scroll" }, el("h3", {}, "Ranking · " + m.domain));
  const rows2 = (data.ranking[m.domain] || []);
  ranking.append(rows2.length ? el("table", {},
    el("thead", {}, el("tr", {}, ["agent", "points", "built", "found", "refuted", "failed challenges"].map((h) => el("th", {}, h)))),
    el("tbody", {}, rows2.map((r) => el("tr", {}, el("td", {}, el("a", { class: "file", href: "#",
      onclick: (e) => { e.preventDefault(); openRecord(r.agent); } }, short(r.agent))),
      [r.points, r.built, r.found, r.refuted, r.failed_challenges].map((v) => el("td", { class: "num" }, v))))))
    : el("p", { class: "empty" }, "No settled results yet."));
  box.append(el("div", { class: "grid2" }, artifacts, ranking));

  if (data.watch && (data.watch.recent.length || data.watch.notifications.length)) {
    const w = el("div", { class: "card" }, el("h3", {}, "Watch: recent agent turns and notifications"));
    for (const n of data.watch.notifications.slice().reverse()) w.append(el("div", { class: "row" }, chip("needs you", "human"), when(n.ts, now), el("span", {}, n.message)));
    for (const t of data.watch.recent.slice().reverse()) w.append(el("div", { class: "row" }, chip(t.kind), when(t.started, now),
      el("span", { class: "meta" }, "mission " + short(t.mission) + " · exit " + t.exit + " · " + t.seconds + " s"),
      data.turns_enabled && t.turn ? el("a", { class: "file", href: "#", onclick: (e) => { e.preventDefault(); openTurn(t.turn); } }, "what it did") : null));
    box.append(w);
  }
}

async function refresh() {
  try {
    const data = await getJSON("api/overview");
    if (!selected && data.missions.length) selected = data.missions[0].id;
    renderOverview(data);
    renderTurns(data).catch(() => {});
    if (selected) renderDetail(await getJSON("api/mission/" + selected), data);
  } catch (err) {
    $("refreshed").textContent = "could not refresh: " + err.message;
  }
}
window.addEventListener("hashchange", () => { selected = location.hash.replace(/^#/, "") || selected; refresh(); });
refresh();
setInterval(() => { if (!$("viewer").open) refresh(); }, 5000);

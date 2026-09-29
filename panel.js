// The panel (served at /; the classic one is still at /classic): a Mission Impossible
// style HUD with plain words, so nothing needs learning. Every key has a visible button.
// Everything is drawn with textContent; nothing from a job board, an email or the model
// becomes HTML. The only links are https job postings, the Gmail search for an email's
// Message-ID, and the panel's own PDFs, the same as the classic panel.
"use strict";

const $ = id => document.getElementById(id);
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function store(k, v){ try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const phone = () => matchMedia("(max-width: 760px)").matches;
const pad = n => String(n).padStart(2, "0");
const SVGNS = "http://www.w3.org/2000/svg";

let toastTimer;
function toast(msg, bad){ const t = $("toast"); t.textContent = msg; t.className = "on" + (bad ? " bad" : ""); clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ""; }, bad ? 5000 : 2600); }

// ---------- server ----------
async function api(path, opts){
  const r = await fetch(path, Object.assign({credentials: "same-origin"}, opts || {}));
  if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || "Error " + r.status); }
  return r.json().catch(() => ({}));
}
const send = (path, method, body) => api(path, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
const fail = e => toast(e.message, true);

// ---------- time ----------
function when(iso){ return iso ? new Date(iso).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : ""; }
function day(iso){ return iso ? new Date(iso).toLocaleDateString([], {month: "short", day: "numeric"}) : ""; }
function ago(iso){
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  if (s < 86400 * 7) return Math.floor(s / 86400) + "d ago";
  return day(iso);
}
// A deadline from an email ("Oct 5th, 2026") as a time, or null when it isn't a date ("8-12 minutes")
function dueAt(text){
  if (!text) return null;
  const s = String(text).replace(/(\d)(st|nd|rd|th)\b/gi, "$1").replace(/\bat\b/gi, " ");
  if (!/\d{4}|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(s)) return null;
  let d = new Date(s);
  if (isNaN(d)) return null;
  if (!/\d:\d/.test(s)) d.setHours(23, 59, 0, 0);  // a day with no time: the end of it
  return d;
}
function tminus(ms){
  if (ms <= 0) return ["Past due", "gone"];
  const s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d >= 1) return ["Due in " + d + "d " + pad(h) + "h " + pad(m) + "m", d < 2 ? "hot" : ""];
  return ["Due in " + pad(h) + ":" + pad(m) + ":" + pad(s % 60), "hot"];
}
function countdown(date){ const e = el("span", "tminus"); e.dataset.due = date.getTime(); tickOne(e); return e; }
function tickOne(e){ const [t, c] = tminus(+e.dataset.due - Date.now()); e.textContent = t; e.className = "tminus " + c; }

// ---------- data ----------
// Each list is kept in this browser too, so the panel paints at once from the last known
// state and the live one replaces it a moment later. The server answers unchanged lists
// with a 304, so polling costs next to nothing.
const D = {jobs: null, apps: null, inbox: null, report: null};
const SRC = {jobs: ["/api/jobs", 8000], apps: ["/api/applications", 30000], inbox: ["/api/inbox", 20000], report: ["/api/report", 600000]};
const RAW = {};
let JOB = new Map(), linkOk = true, linkErr = "";
for (const k in SRC) {
  const raw = store("v2:" + k);
  if (raw) try { D[k] = JSON.parse(raw); RAW[k] = raw; } catch (e) {}
}
indexJobs();

async function load(k){
  try {
    const r = await fetch(SRC[k][0], {credentials: "same-origin"});
    if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || "Error " + r.status); }
    const raw = await r.text();
    linkOk = true; linkErr = "";
    if (raw === RAW[k]) { drawStatus(); return; }
    RAW[k] = raw; D[k] = JSON.parse(raw); store("v2:" + k, raw);
    if (k === "jobs") indexJobs();
    render();
  } catch (e) {
    linkOk = false; linkErr = e.message; drawStatus();
  }
}
function indexJobs(){ JOB = new Map(((D.jobs && D.jobs.jobs) || []).map(j => [j.id, j])); }
const timers = {};
function poll(k){ clearTimeout(timers[k]); load(k).finally(() => { timers[k] = setTimeout(() => poll(k), SRC[k][1]); }); }
function pollAll(){ for (const k in SRC) poll(k); pollAgent(); }
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { for (const k in timers) clearTimeout(timers[k]); } else pollAll();
});
const refresh = (...ks) => { for (const k of ks) poll(k); };

// ---------- vocabulary ----------
const MAIL = {assessment: ["warn", "Assessment"], assessment_done: ["good", "Assessment done"], interview: ["good", "Interview request"],
  scheduled: ["good", "Interview scheduled"], offer: ["good", "Offer"], action: ["warn", "Action needed"], outreach: ["", "Recruiter"],
  rejection: ["bad", "Rejection"], confirmation: ["mute", "Confirmation"], verification: ["warn", "Sign-in code"], other: ["mute", "Other"]};
const STATUS = {new: ["", "Open"], approved: ["", "Approved"], applied: ["good", "Applied"], interview: ["good", "Interview"],
  offer: ["good", "Offer"], rejected: ["bad", "Rejected"], withdrew: ["mute", "Withdrawn"], skipped: ["mute", "Skipped"]};
const chip = (tone, text) => el("span", "chip " + (tone || ""), text);
const statusChip = st => { const s = STATUS[st] || ["mute", st]; return chip(s[0], s[1]); };
function scoreClass(n){ const s = D.jobs || {}; return n >= (s.strong || 72) ? "strong" : n >= (s.good || 60) ? "good" : ""; }
function scoreSpan(n){ return el("span", "sc " + scoreClass(n), n == null ? "--" : String(n)); }
function ring(n){
  const r = el("div", "ring " + scoreClass(n)), svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", "0 0 64 64");
  for (const c of ["bg", "fg"]) { const x = document.createElementNS(SVGNS, "circle"); x.setAttribute("cx", 32); x.setAttribute("cy", 32); x.setAttribute("r", 28); x.setAttribute("class", c); svg.append(x); }
  r.style.setProperty("--p", Math.max(0, Math.min(100, n || 0)));
  r.append(svg, el("b", null, n == null ? "--" : String(n)));
  return r;
}
const who = m => m.company || m.ai_company || m.from || "Unknown";
// Email text as plain words: leftover markup and link addresses are dropped (links in emails are never opened)
const clean = s => String(s || "").replace(/<[^>]*(>|$)/g, " ").replace(/https?:\/\/\S+/g, "[link]").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/[\u200b-\u200d\ufeff]/g, "").replace(/\s{3,}/g, "  ").trim();
const safeUrl = u => typeof u === "string" && u.startsWith("https://") ? u : "";
function gmailUrl(m){
  const ib = D.inbox;
  if (!ib || !ib.gmail || !m.msgid) return "";
  return "https://mail.google.com/mail/?authuser=" + encodeURIComponent(ib.address || "") + "#search/" + encodeURIComponent("rfc822msgid:" + m.msgid);
}
function link(href, text, key, cls){
  const a = el("a", "btn " + (cls || "")); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
  a.append(el("span", null, text + " \u2197")); if (key) a.append(el("kbd", null, "[" + key + "]"));
  return a;
}
function button(text, fn, cls, key){ const b = el("button", "btn " + (cls || "")); b.append(el("span", null, text)); if (key) b.append(el("kbd", null, "[" + key + "]")); b.onclick = fn; return b; }
function busy(b, text){ b.disabled = true; b.firstChild.textContent = text; }
function copy(text, what){ navigator.clipboard.writeText(text).then(() => toast((what || "Text") + " copied"), () => toast("Couldn't copy", true)); }
function inp(tag, value, placeholder){ const i = el(tag, "inp"); if (value != null) i.value = value; if (placeholder) i.placeholder = placeholder; return i; }
function select(options, value){
  const s = el("select", "inp");
  for (const o of options) { const [v, t] = Array.isArray(o) ? o : [o, o || "Choose\u2026"]; const op = el("option", null, t); op.value = v; s.append(op); }
  s.value = value == null ? "" : value;
  return s;
}
function field(label, input, help, from){
  const f = el("div", "field"), l = el("label", null, label);
  if (from) l.append(el("span", "from", from));
  f.append(l, input);
  if (help) f.append(el("div", "help", help));
  return f;
}
function sect(title, count, tone){ const h = el("h3", null, title); if (count != null) h.append(chip(tone || "", String(count))); return h; }
function fold(title, count, tone){ const d = el("details", "fold"), s = el("summary", null, title); if (count != null) s.append(chip(tone || "", String(count))); d.append(s); return d; }

// ---------- places ----------
const PLACES = [
  {id: "home", label: "Home", key: "h", find: ""},
  {id: "jobs", label: "Jobs", key: "j", find: "Search jobs"},
  {id: "apps", label: "Applications", short: "Applied", key: "a", find: "Search applications"},
  {id: "inbox", label: "Inbox", key: "i", find: "Search emails"},
  {id: "agencies", label: "Agencies", key: "e", find: "Search agency jobs"},
  {id: "insights", label: "Insights", key: "r", more: true},
  {id: "assistant", label: "Assistant", key: "c", more: true},
  {id: "settings", label: "Settings", key: "s", more: true},
];
const PLACE = Object.fromEntries(PLACES.map(p => [p.id, p]));
const OLD = {today: "home", brief: "home", missions: "jobs", ops: "apps", intercepts: "inbox", assets: "agencies", intel: "insights", report: "insights", chat: "assistant", tasks: "assistant", handler: "assistant", you: "settings", hq: "settings"};
let place = "home";
// Line icons (24x24, stroked), so each place reads at a glance
const ICONS = {
  home: "M3 11l9-7 9 7M5 9.5V20h5v-6h4v6h5V9.5",
  jobs: "M3 7h18v13H3zM8 7V4h8v3M3 12h18",
  apps: "M21 3L10 14M21 3l-7 18-4-7-7-4z",
  inbox: "M3 13l3-8h12l3 8v7H3zM3 13h5l1.5 2.5h5L16 13h5",
  agencies: "M4 21V4h10v17M14 9h6v12M7 8h4M7 12h4M7 16h4M2 21h20",
  insights: "M4 20V11M10 20V5M16 20v-6M2 20h20",
  assistant: "M4 4h16v12H10l-6 4z M8 9h8M8 12h5",
  settings: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  search: "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM15.5 15.5L21 21",
  clip: "M21 11.5l-8.6 8.6a5 5 0 01-7-7l8.6-8.6a3.5 3.5 0 015 5l-8.6 8.6a2 2 0 01-2.8-2.8l7.9-7.9",
  send: "M5 12h14M13 6l6 6-6 6",
  stop: "M7 7h10v10H7z",
  doc: "M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6",
  warn: "M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
};
function icon(name){
  const svg = document.createElementNS(SVGNS, "svg"), path = document.createElementNS(SVGNS, "path");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("class", "icon");
  path.setAttribute("d", ICONS[name]); svg.append(path); return svg;
}

function buildNav(){
  const side = $("side");
  for (const p of PLACES) {
    if (p.id === "insights") side.append(el("div", "sect more"));
    const b = el("button", "nav" + (p.more ? " more" : "")); b.dataset.place = p.id;
    b.title = p.label + "  (shortcut: g then " + p.key + ")";
    const ic = el("span", "ic"); ic.append(icon(p.id));
    b.append(ic, el("span", "lbl", p.label), el("span", "sl", p.short || p.label), el("span", "n"));
    b.onclick = () => go(p.id);
    side.append(b);
  }
  // On the phone the bottom bar holds the five main places; "More" opens the rest
  const more = el("button", "nav morebtn"); more.dataset.place = "more";
  const mic = el("span", "ic"); mic.append(icon("more"));
  more.append(mic, el("span", "lbl", "More"), el("span", "sl", "More"), el("span", "n"));
  more.onclick = showMore;
  side.append(more);
  const foot = el("div", "foot");
  const help = el("a", null, "Keyboard shortcuts  ?"); help.href = "#"; help.onclick = e => { e.preventDefault(); showHelp(); };
  const old = el("a", null, "Classic panel \u2197"); old.href = "/classic";
  foot.append(help, old);
  side.append(foot);
}
function drawNav(){
  const counts = {
    jobs: D.jobs ? [(D.jobs.review || []).length, "warn"] : null,
    inbox: D.inbox ? [needsYou().length, "warn"] : null,
    agencies: D.jobs ? [(D.jobs.agency_ready || []).length, ""] : null,
    apps: D.apps && place !== "apps" ? [(D.apps.applications || []).filter(x => appUpdate(x) > appsSeen).length, "warn"] : null,
    assistant: AG.pending ? [1, "warn"] : null,
  };
  counts.more = counts.assistant;
  for (const b of document.querySelectorAll(".nav")) {
    b.classList.toggle("on", b.dataset.place === place || (b.dataset.place === "more" && !!(PLACE[place] || {}).more));
    const c = counts[b.dataset.place], n = b.querySelector(".n");
    if (!n) continue;
    n.textContent = c && c[0] ? String(c[0]) : ""; n.className = "n " + (c ? c[1] : "");
  }
}

// ---------- lists ----------
// Each list place: its tabs, the items in a tab (two-line rows), and how one item is shown.
function jobItem(j, extra){
  const f0 = (j.flags || [])[0];
  return {id: j.id, obj: j, kind: "job", t: j.title, r: scoreSpan(j.score),
    m: [f0 && f0.action === "block" ? "blocked: " + f0.title.toLowerCase() : "", j.company, j.location, extra || ago(j.found)].filter(Boolean).join("  \u00b7  "),
    hay: [j.title, j.company, j.location, j.summary, (j.has_skills || []).join(" "), j.level].join(" "),
    sig: j.status + j.prepared + j.docs + (j.flags || []).map(f => f.rule + f.action).join()};
}
const byIds = ids => (ids || []).map(id => JOB.get(id)).filter(Boolean);
const LISTS = {
  jobs: {
    title: "Jobs",
    tabs: [["awaiting", "To review"], ["open", "Open"], ["accepted", "Approved"], ["declined", "Skipped"]],
    items(tab){
      const s = D.jobs; if (!s) return null;
      const all = s.jobs.filter(j => !j.agency);
      const list = tab === "awaiting" ? byIds(s.review) : tab === "open" ? all.filter(j => j.status === "new")
        : tab === "accepted" ? all.filter(j => j.status === "approved") : all.filter(j => j.status === "skipped");
      return list.map(j => jobItem(j));
    },
    empty: {awaiting: ["Nothing to review", "New applications are prepared overnight."], open: ["No open jobs", ""],
            accepted: ["Nothing approved", "Approved applications wait here until the autofill sends them."], declined: ["Nothing skipped", ""]},
    detail: jobDetail, actions: jobActions,
  },
  apps: {
    title: "Applications",
    tabs: [["active", "Active"], ["interview", "Interviews"], ["all", "All"], ["closed", "Rejected"]],
    items(tab){
      const a = D.apps; if (!a) return null;
      const list = a.applications.filter(x => tab === "all" ? true : tab === "interview" ? ["interview", "offer"].includes(x.status)
        : tab === "closed" ? ["rejected", "withdrew"].includes(x.status) : ["applied", "interview", "offer", "approved"].includes(x.status));
      return list.map(x => ({id: x.id, obj: x, kind: "app", t: x.company + "  \u2014  " + x.title, r: statusChip(x.status),
        m: [x.applied_at ? "applied " + day(x.applied_at) : "approved " + day(x.approved_at), x.last_email ? (MAIL[x.last_email.kind] || ["", x.last_email.kind])[1].toLowerCase() + " " + ago(x.last_email.date) : "no reply yet"].join("  \u00b7  "),
        hay: [x.company, x.title, x.location, x.status, x.agency].join(" "), done: ["rejected", "withdrew"].includes(x.status), fresh: appUpdate(x) > appsMarkSince,
        sig: x.status + x.status_at + x.emails + (appUpdate(x) > appsMarkSince)}));
    },
    empty: {active: ["No applications yet", "Applications you send appear here."], interview: ["No interviews yet", ""], all: ["No applications yet", ""], closed: ["No rejections", ""]},
    detail: appDetail, actions: appActions,
  },
  inbox: {
    title: "Inbox",
    tabs: [["needs", "Needs you"], ["all", "All"], ["replies", "Replies"], ["codes", "Codes"]],
    items(tab){
      const ib = D.inbox; if (!ib) return null;
      if (!ib.configured) return [];
      const need = new Set(needsYou().map(m => m.uid));
      const list = (ib.messages || []).filter(m => tab === "needs" ? need.has(m.uid) : tab === "replies" ? !["confirmation", "verification", "other"].includes(m.kind)
        : tab === "codes" ? m.kind === "verification" || m.code : true);
      return list.map(m => ({id: "m" + m.uid, obj: m, kind: "mail", t: who(m) + "  \u2014  " + clean(m.subject), r: el("span", null, ago(m.date)),
        m: [(MAIL[m.kind] || ["", m.kind])[1], m.due ? "due " + m.due : "", m.summary].filter(Boolean).join("  \u00b7  "),
        hay: [who(m), m.subject, m.summary, m.kind, m.title].join(" "), done: m.kind === "confirmation" && !need.has(m.uid),
        sig: String(m.open) + m.done + m.kind + m.summary}));
    },
    empty: {needs: ["Nothing needs you", "Assessments, interview requests and sign-in codes show up here."], all: ["Inbox empty", ""], replies: ["No replies yet", ""], codes: ["No codes", ""]},
    detail: mailDetail, actions: inboxActions,
  },
  agencies: {
    title: "Agencies",
    tabs: [["ready", "Ready"], ["open", "Open"], ["applied", "Applied"]],
    items(tab){
      const s = D.jobs; if (!s) return null;
      const all = s.jobs.filter(j => j.agency);
      const list = tab === "ready" ? byIds(s.agency_ready) : tab === "open" ? all.filter(j => j.status === "new") : all.filter(j => ["applied", "interview", "offer"].includes(j.status));
      return list.map(j => jobItem(j, j.agency));
    },
    empty: {ready: ["No agency jobs ready", "Resume and cover letter are prepared before one shows here."], open: ["Nothing open", ""], applied: ["Nothing applied", ""]},
    detail: jobDetail, actions: agencyActions,
  },
};
// Applications' badge: how many got a reply (not an automatic confirmation or a sign-in code) or a new
// status since you last opened Applications. Those rows are marked while you're there.
let appsSeen = +(store("v2:appsSeen") || 0), appsMarkSince = appsSeen;
if (!appsSeen) { appsSeen = appsMarkSince = Date.now(); store("v2:appsSeen", String(appsSeen)); }  // first visit: nothing is "new"
function appUpdate(x){
  const t = [0];
  if (x.last_email && !["confirmation", "verification"].includes(x.last_email.kind)) t.push(Date.parse(x.last_email.date) || 0);
  if (["interview", "offer", "rejected", "withdrew"].includes(x.status) && x.status_at) t.push(Date.parse(x.status_at) || 0);
  return Math.max(...t);
}
function needsYou(){
  const ms = (D.inbox && D.inbox.messages) || [];
  return ms.filter(m => m.open || (m.code && Date.now() - new Date(m.date) < 2 * 3600e3));
}

// per place: the tab, the filter text and the item under the cursor
const TAB = {}, FILTER = {}, CUR = {};
for (const k in LISTS) TAB[k] = store("v2:tab:" + k) || LISTS[k].tabs[0][0];
let items = [], listSig = "", actsSig = "";

// Rows seen before in this browser don't type themselves out again
let SEEN = new Set();
try { SEEN = new Set(JSON.parse(store("v2:seen") || "[]")); } catch (e) {}
const firstVisit = !SEEN.size;
function markSeen(ids){ let added = false; for (const id of ids) if (!SEEN.has(id)) { SEEN.add(id); added = true; } if (added) store("v2:seen", JSON.stringify([...SEEN].slice(-3000))); }

function currentItems(){
  const L = LISTS[place], raw = L.items(TAB[place]);
  if (!raw) return null;
  const words = (FILTER[place] || "").toLowerCase().split(/\s+/).filter(Boolean);
  return words.length ? raw.filter(it => { const h = String(it.hay).toLowerCase(); return words.every(w => h.includes(w)); }) : raw;
}
function drawTabs(){
  const L = LISTS[place], box = $("tabs"); box.replaceChildren();
  L.tabs.forEach(([id, label], i) => {
    const all = L.items(id), b = el("button", "tab" + (TAB[place] === id ? " on" : ""), label);
    b.title = label + "  (" + (i + 1) + ")"; b.setAttribute("role", "tab");
    if (all) b.append(el("b", null, String(all.length)));
    b.onclick = () => setTab(id);
    box.append(b);
  });
}
function setTab(id){ TAB[place] = id; store("v2:tab:" + place, id); listSig = ""; render(); }

function drawList(){
  const L = LISTS[place];
  $("ltitle").textContent = L.title;
  const its = currentItems();
  drawTabs();
  drawActs();
  const sig = place + "|" + TAB[place] + "|" + (FILTER[place] || "") + "|" + (its ? its.map(i => i.id + i.t + i.m + (i.sig || "")).join("\n") : "-");
  items = its || [];
  $("lcount").textContent = its ? (FILTER[place] ? its.length + " match" + (its.length === 1 ? "" : "es") : "") : "";
  if (sig !== listSig) {
    listSig = sig;
    const box = $("list"); box.replaceChildren();
    if (!its) box.append(emptyBox("Loading", "One moment\u2026"));
    else if (!its.length) {
      const e = FILTER[place] ? ["No match", "Nothing here matches \u201c" + FILTER[place] + "\u201d."] : (L.empty[TAB[place]] || ["Nothing here", ""]);
      box.append(emptyBox(e[0], e[1]));
    }
    const fresh = [];
    for (const it of items) {
      const row = el("div", "row" + (it.done ? " done" : "") + (it.fresh ? " fresh" : "")); row.dataset.id = it.id; row.setAttribute("role", "option");
      if (it.fresh) row.title = "New since you last looked";
      const t = el("span", "t"), r = el("span", "r"), m = el("span", "m", it.m);
      if (it.r) r.append(it.r);
      row.append(el("span", "caret", "\u258C"), t, r, m);
      row.onclick = () => { setCur(it.id); if (phone()) openDetail(); };
      if (!firstVisit && !SEEN.has(it.id) && fresh.length < 8 && !REDUCED) fresh.push([t, it.t]); else t.textContent = it.t;
      box.append(row);
    }
    for (const [node, text] of fresh) typeOut(node, text);
    markSeen(items.map(i => i.id));
  }
  if (!items.find(i => i.id === CUR[place])) CUR[place] = items.length ? items[0].id : null;
  drawCursor(false);
}
// The buttons at the top of a list: run the search, apply to approved, check the inbox
function drawActs(){
  const box = $("lacts"), L = LISTS[place];
  const parts = L.actions ? L.actions() : [];
  const sig = place + JSON.stringify(parts.map(p => p.sig || p.textContent));
  if (sig === actsSig) return;
  actsSig = sig;
  box.replaceChildren(...parts);
  box.hidden = !parts.length;
}
function emptyBox(title, text){ const e = el("div", "empty"); e.append(el("b", null, title), el("span", null, text)); return e; }
// A new item's title types itself out, like a line arriving on a terminal
function typeOut(node, text){
  let i = 0; node.classList.add("typing");
  const step = () => { i = Math.min(text.length, i + 2); node.textContent = text.slice(0, i); if (i < text.length) setTimeout(step, 14); else node.classList.remove("typing"); };
  step();
}
function setCur(id){ CUR[place] = id; drawCursor(true); }
function drawCursor(scroll){
  let shown = null;
  for (const row of $("list").children) {
    const on = row.dataset.id === CUR[place];
    row.classList.toggle("cur", on); row.setAttribute("aria-selected", on);
    if (on) shown = row;
  }
  if (shown && scroll) shown.scrollIntoView({block: "nearest"});
  drawDetail(false);
}
function move(d){
  if (!items.length) return;
  const i = items.findIndex(x => x.id === CUR[place]);
  setCur(items[Math.max(0, Math.min(items.length - 1, (i < 0 ? 0 : i) + d))].id);
}
const curItem = () => items.find(i => i.id === CUR[place]);

// ---------- detail ----------
// The detail pane is only rebuilt when a different item is shown, or the shown one
// changed on the server; never while an answer is being typed into it.
let shownKey = "", editing = false;
function drawDetail(force){
  const it = curItem();
  const key = place + "|" + (it ? it.id + "|" + (it.sig || "") : "-") + "|" + (it ? items.indexOf(it) + "/" + items.length : "");
  if (!force && (key === shownKey || (editing && it && shownKey.startsWith(place + "|" + it.id + "|")))) return;
  shownKey = key; editing = false;
  const box = $("detail"); box.replaceChildren(); box.scrollTop = 0;
  if (place === "inbox" && D.inbox && !D.inbox.configured) { inboxSetup(box); return; }
  if (!it) { box.append(emptyBox("Nothing selected", items.length ? "Pick one from the list." : "")); $("main").classList.remove("open"); return; }
  const i = items.indexOf(it);
  const top = el("div", "dtop"), back = button("\u2039 Back", closeDetail, "back");
  top.append(back, el("span", null, detailLabel(it) + " " + (i + 1) + " of " + items.length));
  box.append(top);
  LISTS[place].detail(it.obj, box);
}
function detailLabel(it){ return it.kind === "mail" ? "Email" : it.kind === "app" ? "Application" : "Job"; }
function openDetail(){ $("main").classList.add("open"); }
function closeDetail(){ $("main").classList.remove("open"); }
function kv(pairs){
  const g = el("dl", "kv");
  for (const [k, v] of pairs) { if (v == null || v === "") continue; const d = el("div"); d.append(el("dt", null, k), el("dd", null, String(v))); d.title = String(v); g.append(d); }
  return g;
}
function head(title, sub, right){ const h = el("div", "dhead"), l = el("div"); l.append(el("h2", null, title), el("div", "sub", sub)); h.append(l); if (right) h.append(right); return h; }
function note(text, tone){ return el("div", "note " + (tone || ""), text); }

// A job's full record (answers, documents, posting, emails) is fetched when it opens; the
// next one in the list is fetched meanwhile, so going through the list is instant.
const DETAIL = new Map();
function getDetail(id, fresh){
  const hit = DETAIL.get(id);
  if (!fresh && hit && Date.now() - hit.at < 90000) return hit.p;
  const p = api("/api/jobs/detail?id=" + encodeURIComponent(id));
  DETAIL.set(id, {p, at: Date.now()});
  p.catch(() => DETAIL.delete(id));
  return p;
}
function prefetchNext(id){ const i = items.findIndex(x => x.id === id), n = items[i + 1]; if (n && n.kind === "job") getDetail(n.id).catch(() => {}); }

// ---------- jobs ----------
function jobDetail(j, box){
  box.append(head(j.title, [j.company, j.location, j.level, j.years != null ? j.years + "+ yrs" : ""].filter(Boolean).join("  \u00b7  "), ring(j.score)));
  if (j.summary) box.append(el("p", "lede", j.summary));
  const chips = el("div", "chips"); chips.style.marginBottom = "16px";
  chips.append(statusChip(j.status));
  if (j.no_sponsorship) chips.append(chip("bad", "Won't sponsor visas")); else if (j.sponsors) chips.append(chip("good", "Sponsors visas"));
  if (j.agency) chips.append(chip("", "via " + j.agency + (j.employment ? " \u00b7 " + j.employment.toLowerCase().replace(/_/g, " ") : "")));
  box.append(chips);
  const has = j.has_skills || [], miss = j.missing_skills || [];
  if (has.length + miss.length) {
    box.append(sect("Skills: you have " + has.length + " of " + (has.length + miss.length)));
    const c = el("div", "chips");
    for (const s of has) c.append(chip("good", s));
    for (const s of miss) c.append(chip("warn", "\u2212 " + s));
    box.append(c);
  }
  if ((j.flags || []).length) {
    box.append(sect("Hiring rules"));
    for (const f of j.flags) {
      const d = el("div", "flag " + (f.action || ""));
      d.append(chip(f.action === "block" ? "bad" : "warn", f.action === "block" ? "Blocked" : f.action === "ask" ? "Check" : "Note"), el("span", null, f.title || f.rule), el("span", "msg", f.msg || ""));
      box.append(d);
    }
    if (j.flags.some(f => f.action === "block")) box.append(note("Blocked jobs can't be approved. If the rule is wrong for this one, change it in Settings \u203a Rules."));
  }
  const more = el("div", "loading dim", "Loading the answers and documents\u2026");
  box.append(more);
  const id = j.id;
  getDetail(id).then(full => {
    if (!more.isConnected) return;
    more.remove();
    jobFull(full, box);
    prefetchNext(id);
  }, e => { more.textContent = "Couldn't load the rest: " + e.message; });
}
const NEXT_STATUS = {
  new: [["skipped", "Skip", "Skipped"], ["applied", "Mark applied", "Marked applied"]],
  approved: [["new", "Back to review", "Moved back to review"], ["applied", "Mark applied", "Marked applied"]],
  applied: [["interview", "Interviewing", "Nice! Marked interviewing"], ["rejected", "Rejected", "Marked rejected"], ["withdrew", "Withdrew", "Marked withdrawn"], ["new", "Back to open", "Moved back"]],
  interview: [["offer", "Got an offer", "Congratulations!"], ["rejected", "Rejected", "Marked rejected"], ["withdrew", "Withdrew or declined", "Marked withdrawn"], ["applied", "Back to applied", "Moved back"]],
  offer: [["withdrew", "Declined", "Marked declined"], ["interview", "Back to interviewing", "Moved back"]],
  rejected: [["new", "Back to open", "Moved back"]], withdrew: [["new", "Back to open", "Moved back"]], skipped: [["new", "Back to open", "Moved back"]],
};
const KIND = {fact: "From your profile", draft: "Drafted by the local model. Check every claim.", you: "Needs your answer", legal: "Consent", file: "File", eeo: "Voluntary"};
function jobFull(j, box){
  const reviewable = j.answers && j.status === "new" && j.auto_apply;
  const acts = el("div", "acts");
  const url = safeUrl(j.apply_url) || safeUrl(j.url);
  if (url) acts.append(link(url, j.agency ? "Apply on the agency site" : "Open application", "o"));
  for (const [st, label, msg] of NEXT_STATUS[j.status] || []) {
    if (reviewable && st === "skipped") continue;  // Skip sits next to Approve below
    acts.append(button(label, () => setJobStatus(j.id, st, msg)));
  }
  box.append(acts);
  if (j.emails && j.emails.length) {
    box.append(sect("Emails about it", j.emails.length));
    for (const m of j.emails) box.append(mailLine(m));
  }
  if (j.auto_apply || j.agency || j.docs) { const d = el("div"); box.append(d); drawDocs(d, j); }
  let decide = null;
  if (reviewable) decide = reviewForm(j, box);
  else if (j.answers) readOnlyAnswers(j, box);
  else if (j.status === "new") {
    box.append(sect("Answers"), note("No answers prepared for this job yet."));
    const b = button("Prepare answers", async () => {
      busy(b, "Preparing, a few minutes\u2026");
      try { await send("/api/jobs/prepare", "POST", {id: j.id}); toast("Preparing answers in the background"); } catch (e) { fail(e); }
    }, "go");
    const a = el("div", "acts"); a.append(b); box.append(a);
  }
  posting(j, box);
  if (decide) box.append(decide);  // the decision bar comes last, so it sticks to the bottom
}
function mailLine(m){
  const k = MAIL[m.kind] || ["", m.kind], d = el("div", "flag");
  d.append(chip(k[0], k[1]), el("span", null, when(m.date)), el("span", "msg", m.summary || clean(m.subject)));
  return d;
}
function posting(j, box){
  const d = fold("The posting");
  d.append(el("div", "quote", j.description || "(no text saved)"));
  box.append(d);
}
async function setJobStatus(id, status, msg){
  try { await send("/api/jobs/status", "POST", {id, status}); toast(msg); } catch (e) { return fail(e); }
  advance(id, status);
}
// After a decision the job leaves this list here at once and the next one opens, so
// going through the morning list is one key or tap per job; the server catches up after.
function advance(id, status){
  DETAIL.delete(id);
  if (place === "apps") { shownKey = ""; RAW.apps = ""; RAW.jobs = ""; refresh("apps", "jobs"); return; }  // it stays in this list with its new status
  const j = JOB.get(id);
  if (j) j.status = status;
  if (D.jobs) { D.jobs.review = (D.jobs.review || []).filter(x => x !== id); D.jobs.agency_ready = (D.jobs.agency_ready || []).filter(x => x !== id); if (status === "approved") D.jobs.approved++; }
  DETAIL.delete(id);
  const at = items.findIndex(x => x.id === id);
  const rest = items.filter(x => x.id !== id);
  CUR[place] = (rest[Math.max(0, at)] || rest[rest.length - 1] || {}).id || null;
  if (!rest.length) toast("That was the last one here.");
  editing = false; listSig = ""; RAW.jobs = ""; RAW.apps = "";
  render();
  refresh("jobs", "apps");
}

function reviewForm(j, box){
  const rows = [], consents = [];
  const byKind = k => j.answers.filter(a => a.kind === k);
  const blocks = (j.flags || []).filter(f => f.action === "block"), asks = (j.flags || []).filter(f => f.action === "ask");
  const left = el("span", "left");
  const count = () => {
    if (blocks.length) { left.textContent = "Blocked by a hiring rule"; left.className = "left bad"; return; }
    const miss = rows.filter(r => r.need && !r.input.value.trim()).length + consents.filter(c => c.required && !c.box.checked).length;
    left.textContent = miss ? miss + " required " + (miss === 1 ? "item" : "items") + " left" : "Ready to approve";
    left.className = "left " + (miss ? "warn" : "good");
  };
  const answerBox = a => {
    const q = el("div", "q"), ql = el("div", "ql");
    if (a.required) ql.append(el("span", "req", "* "));
    ql.append(a.q);
    q.append(ql, el("div", "qk", KIND[a.kind] || a.kind));
    const opts = a.options || [];
    let input;
    if (opts.length && opts.length <= 40) {
      const list = [""].concat(opts);
      if (a.kind !== "you" && a.a && !opts.includes(a.a)) list.push(a.a);
      input = select(list, a.kind === "you" ? "" : a.a || "");
    } else {
      input = inp(a.kind === "draft" || (a.a || "").length > 70 ? "textarea" : "input", a.kind === "you" ? "" : a.a || "");
      if (a.kind === "you") input.placeholder = /Application profile/.test(a.a || "") ? a.a : "Your answer";
    }
    const r = {q: a.q, input, orig: a.kind === "you" ? "" : a.a || "", need: a.required && a.kind === "you"};
    if (r.need) q.classList.add("need");
    const on = () => { editing = true; if (r.need) q.classList.toggle("need", !input.value.trim()); count(); };
    input.addEventListener("input", on); input.addEventListener("change", on);
    rows.push(r); q.append(input);
    return q;
  };
  const need = byKind("you").sort((a, b) => (b.required ? 1 : 0) - (a.required ? 1 : 0));
  if (need.length) { box.append(sect("Needs your answer", need.length, "warn")); for (const a of need) box.append(answerBox(a)); }
  const legal = byKind("legal");
  if (legal.length) {
    const h = sect("Statements you agree to", legal.length);
    if (legal.length > 1) { const all = button("Agree to all " + legal.length, () => { for (const c of consents) c.box.checked = true; editing = true; count(); }, "sm"); h.append(all); }
    box.append(h);
    for (const a of legal) {
      const lab = el("label", "consent"), cb = el("input"), txt = el("div"), ql = el("div", "ql");
      cb.type = "checkbox";
      if (a.required) ql.append(el("span", "req", "* "));
      ql.append(a.q);
      txt.append(ql, el("div", "qk", a.required ? "Required to apply. Only tick it if it's true for you." : "Optional. Left blank unless you tick it."));
      lab.append(cb, txt); box.append(lab);
      cb.onchange = () => { editing = true; count(); };
      consents.push({q: a.q, box: cb, required: a.required});
    }
  }
  const drafts = byKind("draft");
  if (drafts.length) { box.append(sect("Drafted answers", drafts.length)); for (const a of drafts) box.append(answerBox(a)); }
  const facts = byKind("fact");
  if (facts.length) { const d = fold("From your profile", facts.length, "good"); for (const a of facts) d.append(answerBox(a)); box.append(d); }
  const other = j.answers.filter(a => a.kind === "file" || a.kind === "eeo");
  if (other.length) {
    const d = fold("Resume and voluntary questions", other.length);
    for (const a of other) { const q = el("div", "q"); q.append(el("div", "ql", a.q), el("div", "qk", a.kind === "file" ? "Your resume PDF is attached automatically." : "Voluntary. Left blank; set it in Settings \u203a Profile to share it.")); d.append(q); }
    box.append(d);
  }
  if (j.note) box.append(note(j.note));
  // the decision bar stays at the bottom of the pane
  const foot = el("div", "decide");
  const skip = button("Skip", () => setJobStatus(j.id, "skipped", "Skipped"), "", "S");
  const ok = button(asks.length ? "Approve anyway" : "Approve", async () => {
    const answers = rows.filter(r => r.input.value.trim() && r.input.value.trim() !== r.orig).map(r => ({q: r.q, a: r.input.value.trim()}));
    const agreed = consents.filter(c => c.box.checked).map(c => c.q);
    if (asks.length && !confirm("Go ahead despite these hiring rules?\n\n" + asks.map(f => "\u2022 " + f.title + ": " + f.msg).join("\n"))) return;
    ok.disabled = true;
    try { await send("/api/jobs/approve", "POST", {id: j.id, answers, agreed, allow: asks.map(f => f.rule)}); }
    catch (e) { ok.disabled = false; return fail(e); }
    toast("Approved. Send it with Apply to approved.");
    advance(j.id, "approved");
  }, "go", "A");
  skip.dataset.key = "s"; ok.dataset.key = "a";
  if (blocks.length) { ok.disabled = true; ok.title = blocks.map(f => f.msg).join(" "); }
  foot.append(left, skip, ok);
  count();
  return foot;
}
function readOnlyAnswers(j, box){
  for (const [k, title] of [["you", "Needs your answer"], ["legal", "Consents"], ["draft", "Drafted answers"], ["fact", "From your profile"], ["eeo", "Voluntary"], ["file", "Files"]]) {
    const list = j.answers.filter(a => a.kind === k);
    if (!list.length) continue;
    const d = fold(title, list.length, k === "you" ? "warn" : "");
    if (k === "you" || k === "draft") d.open = true;
    for (const a of list) {
      const q = el("div", "q"), ql = el("div", "ql");
      if (a.required) ql.append(el("span", "req", "* "));
      ql.append(a.q); q.append(ql);
      if (a.a) q.append(el("div", "qa", a.a));
      if ((k === "fact" || k === "draft") && a.a) { const b = button("Copy", () => copy(a.a, "Answer"), "sm"); b.style.marginTop = "8px"; q.append(b); }
      d.append(q);
    }
    box.append(d);
  }
  if (j.note) box.append(note(j.note));
}
// The tailored resume and cover letter: open the PDFs, edit the summary and the letter
function docTile(href, title, sub){
  const a = el("a", "doc"); a.href = href; a.target = "_blank"; a.rel = "noopener";
  const t = el("div"); t.append(el("b", null, title), el("span", null, sub));
  a.append(icon("doc"), t);
  return a;
}
function drawDocs(box, j){
  box.replaceChildren(sect("Resume and cover letter"));
  if (j.ai_restricted) box.append(note("This form has an AI-use policy. The autofill will attach your usual resume, no cover letter, and leave drafted answers to you, unless you approve anyway.", "warn"));
  const dc = j.docs;
  if (!dc) {
    box.append(note("No tailored documents yet. Without them the autofill attaches your usual resume and no cover letter."));
    const b = button("Make resume and cover letter", () => makeDocs(j, box, b));
    const a = el("div", "acts"); a.append(b); box.append(a);
    return;
  }
  const t = "&t=" + encodeURIComponent(dc.made || ""), q = "/api/jobs/doc?id=" + encodeURIComponent(j.id) + "&kind=";
  const tiles = el("div", "docs");
  tiles.append(docTile(q + "resume" + t, "Tailored resume", "PDF \u00b7 projects and skills ordered for this job"), docTile(q + "cover" + t, "Cover letter", "PDF \u00b7 drafted by the local model"));
  box.append(tiles);
  for (const w of dc.warnings || []) box.append(note(w, "warn"));
  const d = fold("Edit the summary and the cover letter");
  const sum = inp("textarea", dc.summary || ""), cov = inp("textarea", dc.cover || "");
  sum.rows = 4; cov.rows = 14;
  for (const x of [sum, cov]) x.addEventListener("input", () => { editing = true; });
  const save = button("Save changes", async () => {
    try { await send("/api/jobs/docs", "PUT", {id: j.id, summary: sum.value, cover: cov.value}); } catch (e) { return fail(e); }
    dc.summary = sum.value; dc.cover = cov.value; DETAIL.delete(j.id);
    toast("Saved. The PDFs now use your changes.");
  }, "go");
  const again = button("Write them again", () => { if (confirm("Write a new summary and cover letter? Your edits are replaced.")) makeDocs(j, box, again); });
  const a = el("div", "acts"); a.append(save, again);
  d.append(field("Resume summary", sum, "Your bullets stay as they are; only this summary and the order of projects and skills change per job."),
           field("Cover letter", cov, "Check every claim. Blank lines start new paragraphs."), a);
  box.append(d);
}
async function makeDocs(j, box, b){
  try { await send("/api/jobs/docs", "POST", {id: j.id}); } catch (e) { return fail(e); }
  busy(b, "Writing, about 2 minutes\u2026");
  const was = j.docs && j.docs.made;
  for (let t = 0; t < 40; t++) {  // up to 10 minutes: the model may be busy with a chat
    await new Promise(r => setTimeout(r, 15000));
    if (!box.isConnected) return;
    let fresh;
    try { fresh = await getDetail(j.id, true); } catch (e) { continue; }
    if (fresh.docs && fresh.docs.made !== was) { j.docs = fresh.docs; toast("Resume and cover letter ready"); return drawDocs(box, j); }
  }
  b.disabled = false; b.firstChild.textContent = "Still working. Check back later";
}

// the Jobs list's buttons: send the approved ones, run or stop the search, prepare more
function searchButton(){
  const s = D.jobs;
  if (!s || !s.configured) return null;
  const running = s.progress && s.progress.running;
  const b = button(running ? "Stop the search" : "Run the search now", async () => {
    if (running) {
      if (!confirm("Stop the search? What it found so far is kept; the rest waits for the next run.")) return;
      try { await send("/api/jobs/stop", "POST"); toast("Stopping after the current step"); } catch (e) { return fail(e); }
    } else {
      try { await send("/api/jobs/run", "POST"); toast("Search started"); } catch (e) { return fail(e); }
    }
    setTimeout(() => refresh("jobs"), 700);
  }, "sm " + (running ? "danger" : ""));
  b.sig = "search" + running;
  return b;
}
let nextApply = {n: -1, a: null};
function applyButton(){
  const n = D.jobs ? D.jobs.approved : 0;
  if (!n) return null;
  const a = link("#", "Apply to approved (" + n + ")", null, "sm go");
  a.sig = "apply" + n;
  a.style.display = "none";
  api("/api/jobs/next").then(r => {
    const u = r.job && safeUrl(r.job.apply_url);
    if (u) { a.href = u + "#agent-auto"; a.style.display = ""; a.title = "The autofill opens each approved application, fills it and submits it after a 3-second countdown."; }
  }, () => {});
  return a;
}
function readyButton(n, what){
  const s = D.jobs;
  if (!n || !s || (s.progress && s.progress.running)) return null;
  const b = button("Prepare " + n + " more now", async () => {
    try { await send("/api/jobs/ready", "POST"); toast("Getting them ready. " + what); } catch (e) { return fail(e); }
    setTimeout(() => refresh("jobs"), 700);
  }, "sm");
  b.title = "Writes their answers, tailored resume and cover letter now instead of overnight, about 4 minutes each.";
  b.sig = "ready" + n;
  return b;
}
function jobActions(){ const s = D.jobs; return [applyButton(), s && readyButton(s.not_ready, "They join To review as they finish."), searchButton()].filter(Boolean); }
function agencyActions(){ const s = D.jobs; return [s && readyButton(s.agency_not_ready, "They show up under Ready as they finish.")].filter(Boolean); }

// ---------- applications ----------
function appDetail(x, box){
  box.append(head(x.company, [x.title, x.location].filter(Boolean).join("  \u00b7  "), statusChip(x.status)));
  box.append(kv([["Applied", when(x.applied_at)], ["Approved", when(x.approved_at)], ["Match", x.score], ["Emails", x.emails], ["Replied", x.replied ? "Yes" : "Not yet"], ["Agency", x.agency]]));
  const more = el("div", "loading dim", "Loading the timeline\u2026");
  box.append(more);
  getDetail(x.id).then(j => {
    if (!more.isConnected) return;
    more.remove();
    const acts = el("div", "acts"), url = safeUrl(j.url);
    if (url) acts.append(link(url, "Open posting", "o"));
    for (const [st, label, msg] of (NEXT_STATUS[j.status] || []).filter(s => s[0] !== "new")) acts.append(button(label, () => setJobStatus(j.id, st, msg)));
    box.append(acts);
    // timeline
    box.append(sect("Timeline"));
    const evs = [], sub = j.submission;
    if (j.approved_at) evs.push([j.approved_at, "Approved", ""]);
    if (sub) evs.push([sub.at, sub.exact ? "Submitted by the autofill" : "Applied", sub.exact ? "" : "Marked applied"]);
    for (const m of j.emails || []) { const k = MAIL[m.kind] || ["", m.kind]; evs.push([m.date, k[1], m.summary || clean(m.subject)]); }
    if (j.status_at && ["interview", "offer", "rejected", "withdrew"].includes(j.status)) evs.push([j.status_at, "Marked " + (STATUS[j.status] || ["", j.status])[1].toLowerCase(), ""]);
    evs.sort((a, b) => (a[0] || "").localeCompare(b[0] || ""));
    const tl = el("div", "tl");
    for (const [at, what, extra] of evs) { const e = el("div", "ev"); e.append(el("span", "when", when(at)), el("b", null, what)); if (extra) e.append(el("div", "dim", extra)); tl.append(e); }
    if (!evs.length) tl.append(el("div", "dim", "Nothing yet."));
    box.append(tl);
    // the application as it was sent
    box.append(sect("What was sent"));
    if (!sub) { box.append(note(j.status === "approved" ? "Not sent yet. It's waiting for Apply to approved." : "No record of this application.")); return; }
    box.append(note(sub.exact ? "Exact copy, recorded right before Submit on " + when(sub.at) + "." : "Rebuilt from the prepared answers on " + when(sub.at) + ". What was sent may differ.", sub.exact ? "good" : "warn"));
    if ((sub.files || []).length) {
      const tiles = el("div", "docs");
      for (const f of sub.files) tiles.append(docTile("/api/applications/doc?id=" + encodeURIComponent(j.id) + "&kind=" + encodeURIComponent(f.doc), f.doc === "cover" ? "Cover letter" : f.tailored ? "Tailored resume" : "Resume", f.name));
      box.append(tiles);
    }
    const d = fold("Answers as sent", (sub.fields || []).length);
    for (const f of sub.fields || []) { const q = el("div", "q"); q.append(el("div", "ql", f.q), el("div", "qa" + (f.a ? "" : " dim"), f.a || "(left blank)")); d.append(q); }
    box.append(d);
    if ((sub.consents || []).length) { const c = fold("Statements you agreed to", sub.consents.length); for (const s of sub.consents) c.append(el("div", "q", s)); box.append(c); }
  }, e => { more.textContent = "Couldn't load it: " + e.message; });
}
function appActions(){
  const a = D.apps && D.apps.applications;
  if (!a || !a.length) return [];
  const sent = a.filter(x => x.status !== "approved"), replied = sent.filter(x => x.replied).length;
  const s = el("div", "mini dim", sent.length + " sent \u00b7 " + (sent.length ? Math.round(replied / sent.length * 100) : 0) + "% heard back \u00b7 " + sent.filter(x => Date.now() - new Date(x.applied_at) < 7 * 864e5).length + " this week");
  s.sig = s.textContent;
  return [s];
}

// ---------- inbox ----------
function mailDetail(m, box){
  const k = MAIL[m.kind] || ["", m.kind];
  box.append(head(clean(m.subject) || "(no subject)", [m.from, m.from_addr ? "<" + m.from_addr + ">" : "", when(m.date)].filter(Boolean).join("  "), chip(k[0], k[1])));
  if (m.summary) box.append(el("p", "lede", m.summary));
  const due = dueAt(m.due);
  if (m.code) { box.append(sect("Code")); box.append(el("div", "code", m.code)); }
  box.append(kv([["Company", who(m)], ["Role", m.title], ["Due", m.due], ["When", m.when], ["Status", m.open ? "Needs you" : m.done ? "Done" : ""]]));
  if (due) { box.append(sect("Deadline")); box.append(countdown(due)); }
  const acts = el("div", "acts"), g = gmailUrl(m);
  if (m.open || m.done) { const b = button(m.open ? "Mark done" : "Put back on Needs you", () => setDone(m, !!m.open), m.open ? "go" : "", "d"); b.dataset.key = "d"; acts.append(b); }
  if (g) acts.append(link(g, "Open in Gmail", "o"));
  if (m.code) acts.append(button("Copy code", () => copy(m.code, "Code"), "", "c"));
  if (m.job_id && JOB.get(m.job_id)) acts.append(button("See the application", () => jumpTo("apps", "all", m.job_id)));
  box.append(acts);
  if (m.snippet) { box.append(sect("Email text")); box.append(el("div", "quote", clean(m.snippet))); }
}
async function setDone(m, done){
  try { await send("/api/inbox/done", "POST", {uid: m.uid, done}); } catch (e) { return fail(e); }
  m.open = !done; m.done = done;
  toast(done ? "Done" : "Back on Needs you");
  listSig = ""; RAW.inbox = ""; render(); drawDetail(true);
  refresh("inbox");
}
function jumpTo(p, tab, id){ TAB[p] = tab; FILTER[p] = ""; go(p); setCur(id); }
function inboxActions(){
  const ib = D.inbox;
  if (!ib || !ib.configured) return [];
  const info = el("div", "mini dim", ib.address + " \u00b7 checked " + (ago(ib.checked) || "not yet"));
  info.sig = info.textContent + ib.error;
  const check = button("Check now", async () => {
    busy(check, "Checking\u2026");
    try { await send("/api/inbox/check", "POST"); toast("Checking. New emails appear as the model reads them."); } catch (e) { fail(e); }
    setTimeout(() => { actsSig = ""; refresh("inbox"); }, 4000);
  }, "sm");
  const off = button("Disconnect", async () => {
    if (!confirm("Disconnect the inbox? The saved login and the email list are deleted from the server.")) return;
    try { await api("/api/inbox", {method: "DELETE"}); } catch (e) { return fail(e); }
    refresh("inbox");
  }, "sm");
  const out = [info, check, off];
  if (ib.error) out.unshift(note(ib.error, "bad"));
  return out;
}
function inboxSetup(box){
  box.append(head("Connect the agent's inbox", "An address just for applications"));
  box.append(el("p", "lede", "Every 5 minutes the agent reads new mail without marking it read. Confirmations mark jobs Applied, interview requests and rejections move them along, and sign-in codes show up here and as alerts. Links in emails are never opened. Use an app password (Google Account \u203a Security \u203a App passwords). Your profile's email becomes this address."));
  const addr = inp("input", "", "you.applications@gmail.com"), pw = inp("input", "", "App password");
  addr.type = "email"; addr.autocomplete = "off"; pw.type = "password"; pw.autocomplete = "new-password";
  const b = button("Connect", async () => {
    busy(b, "Checking the login\u2026");
    try { await send("/api/inbox", "POST", {address: addr.value, password: pw.value}); pw.value = ""; toast("Inbox connected"); refresh("inbox"); }
    catch (e) { fail(e); }
    b.disabled = false; b.firstChild.textContent = "Connect";
  }, "go");
  const a = el("div", "acts"); a.append(b);
  box.append(field("Address", addr), field("App password", pw), a);
}

// ---------- full-page places: Insights, Assistant, Settings ----------
// These fill the whole pane and are built once when opened, so polling never wipes a
// half-typed message or setting.
const PAGES = {insights: drawInsights, assistant: drawAssistant, settings: drawSettings};
let pageBuilt = "";
function drawPage(force){
  if (!force && pageBuilt === place) return;
  pageBuilt = place;
  const box = $("detail"); box.replaceChildren(); box.scrollTop = 0;
  PAGES[place](box);
}
function tabsBar(options, current, onPick){
  const bar = el("div", "seg");
  for (const [id, label] of options) {
    const b = el("button", "tab" + (id === current ? " on" : ""), label);
    b.onclick = () => { for (const x of bar.children) x.classList.toggle("on", x === b); onPick(id); };
    bar.append(b);
  }
  return bar;
}

// ---------- insights: the weekly report ----------
let reportWeek = "";
function drawInsights(box){
  const top = el("div", "ptop");
  top.append(el("h1", null, "Insights"));
  const pick = select([["", "Last 7 days"]], reportWeek);
  pick.style.maxWidth = "220px";
  top.append(pick);
  const body = el("div");
  box.append(top, el("p", "dim", "Counted from your jobs and inbox; nothing here is written by the model. Each suggestion says what it rests on."), body);
  body.append(el("div", "dim", "Loading\u2026"));
  api("/api/report" + (reportWeek ? "?end=" + encodeURIComponent(reportWeek) : "")).then(d => {
    const short = iso => new Date(iso + "T12:00:00").toLocaleDateString([], {month: "short", day: "numeric"});
    for (const w of [...(d.saved || [])].reverse()) { const o = el("option", null, short(w.start) + " \u2013 " + short(w.end)); o.value = w.end; pick.append(o); }
    pick.value = reportWeek;
    pick.onchange = () => { reportWeek = pick.value; drawPage(true); };
    body.replaceChildren();
    report(d.report, body);
  }, e => { body.replaceChildren(note("Couldn't load the report: " + e.message, "bad")); });
}
function report(r, box){
  const f = r.found, fw = r.funnel.week, fa = r.funnel.all;
  const vs = (a, b) => b ? " (" + (a >= b ? "+" : "") + Math.round((a - b) / b * 100) + "%)" : "";
  const st = el("div", "stats");
  for (const [v, k] of [[f.total, "Jobs found" + vs(f.total, f.prev_total)], [f.good, "Good matches"], [fw.applied, "Applied"], [fw.replied, "Replies"], [fw.interview, "Interviews"]]) { const d = el("div"); d.append(el("b", null, String(v || 0)), el("span", null, k)); st.append(d); }
  box.append(st);
  const grid = el("div", "cards"); grid.style.marginTop = "14px"; box.append(grid);
  const card = (title, sub, w) => { const c = el("section", "card brk " + (w || "w6")); c.append(el("h2", null, title)); if (sub) c.append(el("p", "dim small", sub)); grid.append(c); return c; };

  const tips = card("Suggestions", "", "w12 tip");
  if (!r.suggestions.length) tips.append(el("div", "dim", "Nothing to change this week."));
  for (const s of r.suggestions) { const t = el("div", "rtip"); t.append(el("b", null, s.title), el("p", null, s.detail), el("p", "faint", s.evidence)); tips.append(t); }

  const days = card("Jobs found each day", "Openings that passed your filters and were scored.");
  const max = Math.max(1, ...f.days.map(d => d.found)), bars = el("div", "bars"), labels = el("div", "bdays");
  for (const d of f.days) {
    const b = el("div", "bar"), rest = el("i"), strong = el("i", "s");
    rest.style.height = ((d.found - d.strong) / max * 100) + "%"; strong.style.height = (d.strong / max * 100) + "%";
    b.title = d.found + " found, " + d.good + " good, " + d.strong + " strong";
    if (d.found) b.append(el("em", null, String(d.found)));
    b.append(rest, strong); bars.append(b);
    labels.append(el("span", null, new Date(d.day + "T12:00").toLocaleDateString([], {weekday: "short"}).toUpperCase()));
  }
  const lg = el("div", "legend"), l1 = el("span"), l2 = el("span"); l1.append(el("i"), "jobs found"); l2.append(el("i", "s"), "strong matches"); lg.append(l1, l2);
  days.append(bars, labels, lg);
  for (const d of f.days.filter(d => d.agency_errors.length)) days.append(el("div", "dim small", day(d.day + "T12:00") + ": couldn't read " + d.agency_errors.map(e => e.slice(7)).join("; ")));
  const filt = Object.entries(f.filtered || {});
  if (filt.length) days.append(el("p", "dim small", "Left out by your filters: " + filt.map(([k, v]) => v + " " + k).join(", ") + "."));

  const kinds = card("What kind of jobs it finds", "Share of this week's jobs, the change from last week, and how many of each fit you.");
  for (const [key, rows] of Object.entries(r.mix)) {
    if (!rows.length) continue;
    kinds.append(el("div", "mixt", r.mix_labels[key]));
    for (const m of rows.slice(0, 6)) {
      const row = el("div", "mrow"), bar = el("div", "mbar"), i = el("i");
      i.style.width = m.share + "%"; bar.append(i);
      const pc = el("span", "pc", m.share + "%");
      if (m.prev_share != null && m.share !== m.prev_share) pc.append(el("span", m.share > m.prev_share ? "up" : "down", (m.share > m.prev_share ? " \u25b2" : " \u25bc") + Math.abs(m.share - m.prev_share)));
      const nm = el("span", "nm", m.name); nm.title = m.name + ": " + m.count + " jobs";
      row.append(nm, bar, pc, el("span", "fit dim", m.good_rate + "% fit"));
      kinds.append(row);
    }
  }

  const sk = card("Skills", "What this week's jobs ask for, and the missing skills that would lift the most jobs over your good line.");
  sk.append(el("div", "mixt", "Most asked"));
  for (const s of r.skills.asked.slice(0, 10)) { const row = el("div", "line"); const rt = el("span"); rt.append(el("span", "dim", s.share + "%  "), chip(s.have ? "good" : "warn", s.have ? "On your resume" : "Missing")); row.append(el("span", null, s.skill), rt); sk.append(row); }
  sk.append(el("div", "mixt", "Gaps worth closing"));
  for (const g of r.skills.gaps.slice(0, 8)) { const row = el("div", "line"); row.append(el("span", null, g.skill), el("span", "dim", (g.lift ? "+" + g.lift + " good matches \u00b7 " : "") + "asked in " + g.asked)); sk.append(row); }
  sk.append(el("p", "dim small", "Only add a skill to your resume if you've really used it."));

  const ap = card("How your applications are doing", "Groups under " + r.min_group + " applications are dimmed: too few to compare.");
  const tbl = el("table", "rtable"), hr = el("tr");
  for (const h of ["", "All time", "This week"]) hr.append(el("th", null, h));
  tbl.append(hr);
  const pct = (a, b) => b ? " (" + Math.round(a / b * 100) + "%)" : "";
  for (const [k, label] of [["applied", "Sent"], ["replied", "Replied"], ["interview", "Assessment or interview"], ["rejected", "Rejected"], ["offer", "Offer"], ["quiet", "No reply in 14+ days"]]) {
    const tr = el("tr"); tr.append(el("td", null, label), el("td", null, fa[k] + (k === "applied" ? "" : pct(fa[k], fa.applied))), el("td", null, fw[k] + (k === "applied" ? "" : pct(fw[k], fw.applied)))); tbl.append(tr);
  }
  if (fa.median_wait != null) { const tr = el("tr"); tr.append(el("td", null, "Typical days to first reply"), el("td", null, String(fa.median_wait)), el("td", null, fw.median_wait != null ? String(fw.median_wait) : "\u2013")); tbl.append(tr); }
  ap.append(tbl);
  for (const [key, label] of [["score", "By match strength"], ["level", "By level"], ["source", "By where it's posted"], ["role", "By kind of role"]]) {
    const rows = r.funnel.by[key]; if (!rows || !rows.length) continue;
    ap.append(el("div", "mixt", label));
    const t = el("table", "rtable"), h = el("tr");
    for (const x of ["", "Sent", "Replied", "Interview"]) h.append(el("th", null, x));
    t.append(h);
    for (const g of rows) { const tr = el("tr", g.small ? "few" : ""); tr.append(el("td", null, g.name), el("td", null, String(g.n)), el("td", null, g.reply_rate + "%"), el("td", null, g.interview_rate + "%")); t.append(tr); }
    ap.append(t);
  }
}

// ---------- assistant: chat and tasks ----------
let asstTab = store("v2:asst") || "chat";
function drawAssistant(box){
  const top = el("div", "ptop");
  top.append(el("h1", null, "Assistant"), tabsBar([["chat", "Chat"], ["tasks", "Tasks"]], asstTab, t => { asstTab = t; store("v2:asst", t); drawPage(true); }));
  box.append(top);
  const body = el("div", "asst"); box.append(body);
  if (asstTab === "tasks") drawTasks(body); else drawChat(body);
}
// Attachments: files are turned into text on the server first, so the page can show how
// long the model will take to read them. Chat puts the text in the message (first 16,000
// characters); Tasks saves each file into the agent's workspace.
const READ_RATE = {better: 17, faster: 35};  // tokens per second, measured on the server
async function toBase64(file){
  const bytes = new Uint8Array(await file.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
function composer(placeholder, hint, onSend, forChat){
  const wrap = el("div", "composer brk"), files = [], chips = el("div", "cfiles");
  const ta = el("textarea", "inp"); ta.rows = 1; ta.placeholder = placeholder; ta.setAttribute("enterkeyhint", "send");
  const pickr = el("input"); pickr.type = "file"; pickr.multiple = true; pickr.hidden = true;
  const attach = el("button", "cbtn"); attach.title = "Attach files"; attach.setAttribute("aria-label", "Attach files"); attach.append(icon("clip"));
  const go = el("button", "cbtn send"); go.title = "Send (Enter)"; go.setAttribute("aria-label", "Send"); go.append(icon("send"));
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 200) + "px"; };
  const paint = () => {
    chips.replaceChildren();
    let left = 16000;
    for (const f of files) {
      let label = f.name;
      if (f.reading) label += " \u00b7 reading\u2026";
      else if (forChat) {
        const used = Math.min(f.chars, Math.max(left, 0)); left -= used;
        const s = Math.round(used / 4 / READ_RATE[chatModel]);
        label += " \u00b7 " + f.chars.toLocaleString() + " chars" + (used < f.chars ? ", first " + used.toLocaleString() + " used" : "") + " \u00b7 " + (s < 60 ? "~" + Math.max(s, 1) + " s" : "~" + Math.round(s / 60) + " min") + " to read";
      } else label += " \u00b7 " + f.chars.toLocaleString() + " chars, saved to the workspace";
      const c = el("span", "chip"), x = el("button", "x", "\u00d7");
      x.setAttribute("aria-label", "Remove " + f.name);
      x.onclick = () => { files.splice(files.indexOf(f), 1); paint(); };
      c.append(label, x); chips.append(c);
    }
    chips.hidden = !files.length;
  };
  pickr.onchange = async () => {
    const picked = [...pickr.files]; pickr.value = "";
    for (const file of picked) {
      if (file.size > 5e6) { toast(file.name + " is over 5 MB.", true); continue; }
      const f = {name: file.name, reading: true, chars: 0, text: ""};
      files.push(f); paint();
      try { const d = await send("/api/extract", "POST", {name: file.name, data: await toBase64(file)}); Object.assign(f, {name: d.name, text: d.text, chars: d.chars, reading: false}); }
      catch (e) { files.splice(files.indexOf(f), 1); toast(file.name + ": " + e.message, true); }
      paint();
    }
  };
  attach.onclick = () => pickr.click();
  const submit = () => {
    if (forChat && chatAbort) { chatAbort.abort(); return; }
    const text = ta.value.trim();
    if (!text && !files.length) return;
    if (files.some(f => f.reading)) return toast("Still reading a file.");
    const list = files.map(f => ({name: f.name, text: f.text}));
    ta.value = ""; grow(); files.length = 0; paint();
    onSend(text, list);
  };
  go.onclick = submit;
  ta.addEventListener("input", grow);
  ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); } });
  const row = el("div", "crow"); row.append(attach, ta, go);
  wrap.append(chips, row, pickr);
  if (hint) wrap.append(el("div", "chint dim", hint));
  paint();
  return {wrap, ta, go};
}

let chatId = store("chat") || "", chatAbort = null, chatModel = store("chatmodel") || "better", chats = [];
// Light formatting for replies: code blocks, **bold**, `code` and # headings, built from
// text nodes and elements (never HTML)
function inline(parent, text){
  for (const piece of text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/)) {
    if (/^\*\*[^*]+\*\*$/.test(piece)) { const b = el("strong"); inline(b, piece.slice(2, -2)); parent.append(b); }
    else if (/^`[^`]+`$/.test(piece)) parent.append(el("code", null, piece.slice(1, -1)));
    else if (piece) parent.append(piece);
  }
}
function rich(box, text){
  box.replaceChildren();
  const fence = /```[^\n]*\n?([\s\S]*?)(?:```|$)/g;
  let at = 0, m;
  const prose = t => t.split("\n").forEach((ln, i) => {
    if (i) box.append("\n");
    const h = ln.match(/^#{1,6}\s+(.*)$/);
    if (h) box.append(el("strong", null, h[1])); else inline(box, ln);
  });
  while ((m = fence.exec(text))) { prose(text.slice(at, m.index)); box.append(el("pre", null, m[1].replace(/\n$/, ""))); at = fence.lastIndex; }
  prose(text.slice(at));
}
function drawChat(body){
  const side = el("div", "chatside"), main = el("div", "chatmain");
  body.classList.add("chat"); body.append(side, main);
  const newBtn = button("+ New chat", () => { if (!chatAbort) openChat(""); }, "sm");
  const list = el("div", "chatlist");
  side.append(newBtn, list);
  const bar = el("div", "chatbar");
  const title = el("b", "ctitle", "New chat");
  const model = tabsBar([["better", "8B \u00b7 smart"], ["faster", "4B \u00b7 fast"]], chatModel, m => { chatModel = m; store("chatmodel", m); });
  model.title = "Which model answers";
  const del = button("Delete", async () => {
    if (!chatId || !confirm("Delete this conversation?")) return;
    try { await api("/api/chats/" + encodeURIComponent(chatId), {method: "DELETE"}); } catch (e) { return fail(e); }
    await loadChats(); openChat("");
  }, "sm");
  bar.append(title, model, del);
  const msgs = el("div", "msgs");
  const comp = composer("Message the assistant", "Runs on your server, offline. Say \u201cremember that \u2026\u201d to save a fact. Shift+Enter for a new line.", sendChat, true);
  main.append(bar, msgs, comp.wrap);
  const near = () => msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 120;
  const add = (role, text, files) => {
    const d = el("div", "msg " + role);
    if (role === "assistant") rich(d, text); else d.textContent = text;
    if (files && files.length) d.append(el("div", "files dim", "\u{1F4CE} " + files.map(f => f.name).join(", ")));
    msgs.append(d); return d;
  };
  const hello = () => {
    const h = el("div", "hello");
    h.append(el("h2", null, "What can I help with?"), el("p", "dim", "Replies are written on your own server, with no internet access."));
    for (const s of ["Write a follow-up email after an interview", "Explain a Python error I paste", "Make a study plan for system design", "Rewrite my resume bullet to sound stronger"]) {
      const b = button(s, () => { comp.ta.value = s; comp.ta.focus(); }, "sm"); h.append(b);
    }
    msgs.append(h);
  };
  const paintList = () => {
    list.replaceChildren();
    if (!chats.length) list.append(el("div", "dim small", "No conversations yet."));
    for (const c of chats) { const b = el("button", "citem" + (c.id === chatId ? " on" : ""), c.title); b.title = c.title; b.onclick = () => { if (!chatAbort) openChat(c.id); }; list.append(b); }
  };
  async function loadChats(){ try { chats = await api("/api/chats"); } catch (e) { chats = []; } if (chatId && !chats.some(c => c.id === chatId)) chatId = ""; paintList(); }
  async function openChat(id){
    chatId = id; store("chat", id);
    msgs.replaceChildren(); del.hidden = !id;
    const c = chats.find(x => x.id === id); title.textContent = c ? c.title : "New chat";
    paintList();
    if (!id) return hello();
    try { const d = await api("/api/chats/" + encodeURIComponent(id)); for (const m of d.messages) add(m.role, m.text || m.content, m.files); }
    catch (e) { return openChat(""); }
    msgs.scrollTop = msgs.scrollHeight;
  }
  async function sendChat(text, files){
    if (chatAbort) return;
    if (!chatId) msgs.replaceChildren();
    add("user", text || (files.length > 1 ? "Summarize the attached files." : "Summarize the attached file."), files);
    const out = add("assistant", "Thinking. After a pause or a model switch, the first reply can take a minute.");
    out.classList.add("typing");
    msgs.scrollTop = msgs.scrollHeight;
    chatAbort = new AbortController();
    comp.go.replaceChildren(icon("stop")); comp.go.title = "Stop"; comp.go.classList.add("stop");
    let got = "";
    try {
      const r = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, credentials: "same-origin",
        body: JSON.stringify({message: text, chat_id: chatId, model: chatModel, files}), signal: chatAbort.signal});
      if (!r.ok) { const t = await r.json().catch(() => ({})); out.textContent = t.detail || "Error " + r.status; out.classList.remove("typing"); out.classList.add("err"); return; }
      const id = r.headers.get("X-Chat-Id") || "", isNew = id !== chatId;
      chatId = id; store("chat", id);
      const reader = r.body.getReader(), dec = new TextDecoder();
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        const follow = near();
        got += dec.decode(value, {stream: true});
        rich(out, got); out.classList.remove("typing");
        if (follow) msgs.scrollTop = msgs.scrollHeight;
      }
      if (isNew) { await loadChats(); const c = chats.find(x => x.id === chatId); title.textContent = c ? c.title : ""; del.hidden = false; }
    } catch (e) {
      out.classList.remove("typing");
      rich(out, got + (e.name === "AbortError" ? " (stopped)" : "\n(connection lost)"));
    } finally {
      chatAbort = null;
      comp.go.replaceChildren(icon("send")); comp.go.title = "Send (Enter)"; comp.go.classList.remove("stop");
    }
  }
  loadChats().then(() => openChat(chatId));
}

// Tasks: the agent's tool loop on the server. Anything with side effects waits for
// Approve; the approval shows up in the top bar wherever you are.
const AG = {last: 0, events: [], status: "idle", task: "", pending: null, box: null};
async function pollAgent(){
  clearTimeout(timers.agent);
  try {
    const s = await api("/api/state?since=" + AG.last);
    if (s.seq < AG.last) {  // the log was cleared: start over
      AG.last = 0; AG.events = [];
      if (AG.box && AG.box.isConnected) AG.box.replaceChildren();
      return pollAgent();
    }
    for (const e of s.events) { AG.events.push(e); AG.last = Math.max(AG.last, e.n); if (AG.box && AG.box.isConnected) paintEvent(AG.box, e, true); }
    const was = AG.pending && AG.pending.id;
    AG.status = s.status; AG.task = s.task; AG.pending = s.pending;
    if ((AG.pending && AG.pending.id) !== was) { paintPending(); drawNav(); }
    drawAgentStatus();
  } catch (e) {}
  if (!document.hidden) timers.agent = setTimeout(pollAgent, place === "assistant" || AG.status !== "idle" ? 1500 : 5000);
}
function drawAgentStatus(){
  const b = $("approval");
  b.hidden = !AG.pending || (place === "assistant" && asstTab === "tasks");
  const st = document.querySelector(".tstatus");
  if (st) st.textContent = AG.pending ? "Waiting for your approval" : AG.status === "idle" ? "Idle" : AG.status + (AG.task ? ": " + AG.task : "");
}
$("approval").onclick = () => { asstTab = "tasks"; store("v2:asst", "tasks"); pageBuilt = ""; go("assistant"); };
function paintEvent(box, ev, scroll){
  const empty = box.querySelector(".empty"); if (empty) empty.remove();
  const d = el("div", "tev " + ev.kind);
  if (ev.kind === "task") d.append(el("b", null, ev.text));
  else if (ev.kind === "thought") d.append(el("div", "dim", "Step " + ev.step + " \u00b7 " + ev.stats + " \u2014 " + ev.text));
  else if (ev.kind === "action") { d.append(el("div", "tool", ev.tool + (ev.auto ? " \u00b7 auto-approved, read-only" : "")), el("pre", null, ev.arg)); if (ev.content) d.append(el("pre", null, ev.content)); }
  else if (ev.kind === "result") { const f = fold("Result"); f.append(el("pre", null, ev.text)); d.append(f); }
  else if (ev.kind === "rejected") d.append(el("div", "bad", "Denied" + (ev.text ? ": " + ev.text : "")));
  else if (ev.kind === "answer") d.append(el("div", "ans", ev.text));
  else d.append(el("div", "bad", ev.text));
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.append(d);
  if (scroll && near) box.scrollTop = box.scrollHeight;
}
function paintPending(){
  drawAgentStatus();
  const card = document.querySelector(".pending");
  if (!card) return;
  const p = AG.pending;
  card.hidden = !p;
  card.replaceChildren();
  if (!p) return;
  const h = el("div", "ph"); h.append(icon("warn"), el("b", null, "Approve this action?"));
  const reason = inp("input", "", "Reason if denying (optional)");
  const decide = approve => { const id = p.id; AG.pending = null; paintPending(); drawNav(); send("/api/decision", "POST", {id, approve, reason: reason.value}).catch(fail).then(pollAgent); };
  const a = el("div", "acts");
  a.append(button("Deny", () => decide(false), "danger"), button("Approve", () => decide(true), "go"));
  card.append(h, el("div", "tool", p.tool), el("pre", null, p.arg));
  if (p.content) card.append(el("pre", null, p.content));
  card.append(reason, a);
}
function drawTasks(body){
  const bar = el("div", "chatbar");
  bar.append(el("span", "tstatus dim"), button("Stop", () => send("/api/stop", "POST").catch(fail).then(pollAgent), "sm danger"),
    button("Clear", () => send("/api/clear", "POST").then(() => { AG.last = 0; AG.events = []; drawPage(true); pollAgent(); }, fail), "sm"));
  const pending = el("div", "pending brk warn"); pending.hidden = true;
  const log = el("div", "tlog");
  AG.box = log;
  if (!AG.events.length) { const e = emptyBox("No tasks yet", "Ask the agent to do something on the server: check disk space, organize files, summarize a log. Anything with side effects waits for your approval."); log.append(e); }
  for (const e of AG.events) paintEvent(log, e, false);
  const comp = composer("Give the agent a task", "", (text, files) => send("/api/task", "POST", {task: text, files}).then(pollAgent, fail), false);
  body.classList.add("tasks");
  body.append(bar, pending, log, comp.wrap);
  paintPending();
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  pollAgent();
}

// ---------- settings ----------
let setTabId = store("v2:settings") || "profile";
function drawSettings(box){
  const top = el("div", "ptop");
  top.append(el("h1", null, "Settings"), tabsBar([["profile", "Profile"], ["rules", "Hiring rules"], ["workday", "Workday"], ["memory", "Memory"], ["app", "App"]], setTabId,
    t => { setTabId = t; store("v2:settings", t); drawPage(true); }));
  const body = el("div", "settings");
  box.append(top, body);
  ({profile: drawProfile, rules: drawRules, workday: drawWorkday, memory: drawMemory, app: drawApp})[setTabId](body);
}
function saveBar(onSave, label){
  const bar = el("div", "savebar"), msg = el("span", "dim");
  const b = button(label || "Save", async () => { b.disabled = true; try { await onSave(); msg.textContent = "Saved \u2713"; } catch (e) { fail(e); } b.disabled = false; }, "go");
  bar.append(msg, b);
  return {bar, dirty: () => { msg.textContent = "Unsaved changes"; }};
}
// Profile: everything application forms ask for. It saves itself a moment after typing
// stops; what's still empty comes first.
function drawProfile(body){
  body.append(el("div", "dim", "Loading\u2026"));
  api("/api/jobs/profile").then(p => {
    body.replaceChildren();
    const inputs = [], qs = [];
    const status = el("span", "dim"), bar = el("div", "pbar"), fillI = el("i"); bar.append(fillI);
    const stat = el("span", "dim");
    const headc = el("section", "card brk w12");
    const hl = el("div", "line"); hl.append(el("b", null, "Your application profile"), status);
    headc.append(hl, bar, stat, el("p", "dim small", "Everything application forms ask for, built from the questions on real forms. The autofill and the prepared answers use it. Consents are never answered from here. Changes save as you go."));
    body.append(headc);
    let timer = null, chain = Promise.resolve();
    const progress = () => { const n = inputs.filter(i => i.value.trim()).length; fillI.style.width = (inputs.length ? n / inputs.length * 100 : 0) + "%"; stat.textContent = n + " of " + inputs.length + " filled"; };
    const save = async () => {
      const values = {}, answers = [];
      for (const i of inputs) values[i.dataset.key] = i.value.trim();
      for (const i of qs) if (i.value.trim()) answers.push({q: i.dataset.q, a: i.value.trim()});
      status.textContent = "Saving\u2026";
      try { await send("/api/jobs/profile", "PUT", {values, answers}); status.textContent = "Saved \u2713"; }
      catch (e) { status.textContent = ""; toast("Couldn't save: " + e.message, true); }
    };
    const soon = ms => { clearTimeout(timer); timer = setTimeout(() => { chain = chain.then(save); }, ms); };
    const wire = i => { i.addEventListener("input", () => { status.textContent = ""; progress(); soon(1200); }); i.addEventListener("change", () => { progress(); soon(0); }); };
    const make = (f, value) => {
      const i = f.choices ? select([""].concat(f.choices, value && !f.choices.includes(value) ? [value] : []), value) : inp("input", value);
      i.dataset.key = f.key; i.autocomplete = "off"; inputs.push(i); wire(i);
      return i;
    };
    const empty = [];
    for (const s of p.form) for (const f of s.fields) if (!String(p.profile[f.key] || "").trim()) empty.push([s.section, f]);
    if (empty.length) {
      body.append(sect("Still empty", empty.length, "warn"), el("p", "dim small", "Forms ask for these. Fill what applies; leave the rest."));
      const g = el("div", "fgrid");
      for (const [sec, f] of empty) g.append(field(f.label, make(f, ""), f.help, sec));
      body.append(g);
    }
    for (const s of p.form) {
      const filled = s.fields.filter(f => String(p.profile[f.key] || "").trim());
      if (!filled.length) continue;
      const d = fold(s.section, filled.length + " of " + s.fields.length), g = el("div", "fgrid");
      for (const f of filled) g.append(field(f.label, make(f, p.profile[f.key]), f.help));
      d.append(g); body.append(d);
    }
    body.append(sect("Questions it still can't answer", p.unanswered.length, p.unanswered.length ? "warn" : ""));
    body.append(el("p", "dim small", p.unanswered.length ? "From the " + p.prepared_jobs + " jobs with prepared answers, most common first. Answer once and every form that asks gets it. Leave empty to skip." : "None right now. Questions show up here as jobs get prepared."));
    for (const u of p.unanswered) {
      const i = u.options.length && u.options.length <= 12 ? select([""].concat(u.options), "") : inp(u.options.length ? "input" : "textarea", "");
      i.dataset.q = u.q; qs.push(i); wire(i);
      body.append(field(u.q, i, u.jobs + (u.jobs === 1 ? " job" : " jobs") + ": " + u.companies.join(", ") + (u.options.length > 12 ? ". Choices include " + u.options.slice(0, 6).join(", ") : "")));
    }
    body.append(sect("Your saved answers", p.answers.length));
    if (!p.answers.length) body.append(el("p", "dim small", "None yet. Clear one to remove it."));
    for (const a of p.answers) { const i = inp("textarea", a.a); i.dataset.q = a.q; qs.push(i); wire(i); body.append(field(a.q, i)); }
    progress();
    addEventListener("pagehide", () => { if (timer) { clearTimeout(timer); save(); } }, {once: true});
  }, e => body.replaceChildren(note("Couldn't load the profile: " + e.message, "bad")));
}
function drawRules(body){
  body.append(el("div", "dim", "Loading\u2026"));
  api("/api/jobs/rules").then(d => {
    body.replaceChildren();
    const st = d.settings;
    body.append(el("p", "lede", "Employers reject or hold applications that break their rules: duplicates, too many at one company, reapplying too soon, graduation windows, AI-use policies. Every application is checked against these before review, at Approve and again right before it's sent. Block keeps it out of review, Ask makes you confirm, Warn shows a note."));
    const sv = saveBar(async () => {
      const actions = {}, limits = {}, notes = {};
      for (const s of body.querySelectorAll("select[data-rule]")) actions[s.dataset.rule] = s.value;
      for (const r of caps.children) { const [c, m, dd] = r.querySelectorAll("input"); if (c.value.trim()) limits[c.value.trim()] = {max: +m.value || 3, days: +dd.value || 30}; }
      for (const r of refs.children) { const [c, n] = r.querySelectorAll("input"); if (c.value.trim() && n.value.trim()) notes[c.value.trim()] = n.value.trim(); }
      await send("/api/jobs/rules", "PUT", {actions, limits, notes, limit: {max: +dmax.value || 3, days: +ddays.value || 30}, cooldown_days: +cool.value || 180});
      RAW.jobs = ""; refresh("jobs"); toast("Rules saved");
    }, "Save rules");
    const acts = [["block", "Block"], ["ask", "Ask"], ["warn", "Warn"], ["off", "Off"]];
    for (const r of d.rules) {
      const row = el("div", "rrow"), t = el("div");
      t.append(el("b", null, r.title), el("div", "help", r.help));
      const s = select(acts.map(([v, l]) => [v, l + (v === r.default ? " (default)" : "")]), st.actions[r.id] || r.default);
      s.dataset.rule = r.id; s.onchange = sv.dirty;
      row.append(t, s); body.append(row);
    }
    const num = (v, label) => { const i = inp("input", v); i.type = "number"; i.min = "1"; i.setAttribute("aria-label", label); i.oninput = sv.dirty; return i; };
    const kvRow = (box, cells) => {
      const row = el("div", "kvrow");
      for (const [v, ph, type] of cells) { const i = type ? num(v, ph) : inp("input", v, ph); i.placeholder = ph; i.oninput = sv.dirty; row.append(i); }
      const x = button("\u00d7", () => { row.remove(); sv.dirty(); }, "sm"); x.setAttribute("aria-label", "Remove"); row.append(x);
      box.append(row);
    };
    body.append(sect("Company caps"), el("p", "dim small", "Most companies: at most this many applications in this many days. Add companies with their own caps."));
    const dmax = num(st.limit.max, "Most applications"), ddays = num(st.limit.days, "Days");
    const def = el("div", "kvrow"); def.append(el("span", null, "Default: at most"), dmax, el("span", null, "in days"), ddays); body.append(def);
    const caps = el("div"); body.append(caps);
    for (const [k, v] of Object.entries(st.limits)) kvRow(caps, [[k, "Company"], [v.max, "Max", 1], [v.days, "Days", 1]]);
    const a1 = el("div", "acts"); a1.append(button("Add a company", () => kvRow(caps, [["", "Company"], [3, "Max", 1], [30, "Days", 1]]), "sm")); body.append(a1);
    body.append(sect("Cooldown after a rejection"));
    const cool = num(st.cooldown_days, "Cooldown days");
    const cr = el("div", "kvrow"); cr.append(el("span", null, "Days to wait after a rejection that followed interviews"), cool); body.append(cr);
    body.append(sect("Referrals and agencies"), el("p", "dim small", "Companies where someone referred you or a recruiter submitted you. The agent won't apply there directly."));
    const refs = el("div"); body.append(refs);
    for (const [k, v] of Object.entries(st.notes)) kvRow(refs, [[k, "Company"], [v, "Referred by Jane Doe / submitted by an agency"]]);
    const a2 = el("div", "acts"); a2.append(button("Add a company", () => kvRow(refs, [["", "Company"], ["", "Referred by Jane Doe / submitted by an agency"]]), "sm")); body.append(a2);
    body.append(sv.bar);
  }, e => body.replaceChildren(note("Couldn't load the rules: " + e.message, "bad")));
}
function drawWorkday(body){
  body.append(el("div", "dim", "Loading\u2026"));
  api("/api/workday").then(w => {
    body.replaceChildren();
    body.append(el("p", "lede", "Every company on Workday needs its own account. The autofill creates one the first time it applies there, and signs in after that, with your applications email and this password. Workday usually emails a link to verify a new account; it shows up in Inbox. The password stays on your server and is only given to the autofill on Workday pages."));
    const chips = el("div", "chips"); chips.append(chip(w.has_password ? "good" : "warn", w.has_password ? "Password set" : "No password yet"), chip("", w.accounts.length + (w.accounts.length === 1 ? " account" : " accounts") + " created"));
    body.append(chips);
    const pw = inp("input", "", "At least 12 characters: upper, lower, number, symbol"); pw.type = "password"; pw.autocomplete = "new-password";
    const help = el("div", "help", "Generate makes a strong one and shows it once so you can save it in your password manager.");
    const sv = saveBar(async () => {
      const b = {accept_terms: terms.checked}; if (pw.value) b.password = pw.value;
      await send("/api/workday", "PUT", b); pw.value = ""; pw.type = "password"; toast("Saved"); drawPage(true);
    });
    const gen = button("Generate", () => {
      const sets = ["ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%^*-_=+"], all = sets.join("");
      const rnd = n => { const a = new Uint32Array(1); crypto.getRandomValues(a); return a[0] % n; };
      let p = sets.map(x => x[rnd(x.length)]);
      while (p.length < 18) p.push(all[rnd(all.length)]);
      for (let i = p.length - 1; i > 0; i--) { const j = rnd(i + 1); [p[i], p[j]] = [p[j], p[i]]; }
      pw.value = p.join(""); pw.type = "text";
      help.textContent = "Save this in your password manager now; it isn't shown again after you save.";
      sv.dirty();
    }, "sm");
    pw.oninput = sv.dirty;
    const row = el("div", "kvrow"); row.append(pw, gen);
    const f = field("Password for Workday accounts", row, null); f.append(help);
    const lab = el("label", "consent"), terms = el("input"), txt = el("div");
    terms.type = "checkbox"; terms.checked = w.accept_terms; terms.onchange = sv.dirty;
    txt.append(el("div", "ql", "Agree to Workday account terms for me"), el("div", "qk", "Lets the autofill tick the terms box when it creates an account, and Workday's standard terms box on the voluntary disclosures page. Any other agreement still needs your own tick in review."));
    lab.append(terms, txt);
    body.append(f, lab, sv.bar);
  }, e => body.replaceChildren(note("Couldn't load: " + e.message, "bad")));
}
function drawMemory(body){
  body.append(el("p", "lede", "Facts the assistant knows about you, one per line. They go at the start of every chat and task, so keep them short. In a chat, \u201cremember that \u2026\u201d adds one."));
  const ta = inp("textarea", ""); ta.rows = 14; ta.spellcheck = false;
  let max = 2000;
  const sv = saveBar(async () => { await send("/api/memory", "PUT", {text: ta.value}); toast("Memory saved"); }, "Save memory");
  const cnt = sv.bar.firstChild;
  ta.oninput = () => { cnt.textContent = ta.value.length + " / " + max + " characters"; };
  body.append(ta, sv.bar);
  api("/api/memory").then(m => { ta.value = m.text; max = m.max; ta.oninput(); }, fail);
}
// Alerts: web push to this device. iPhones only allow them in the home-screen app.
const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function b64ToBytes(s){ const raw = atob((s + "=".repeat((4 - s.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from(raw, c => c.charCodeAt(0)); }
async function alertState(){
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return "unsupported";
  const reg = await navigator.serviceWorker.register("/sw.js");
  const sub = await reg.pushManager.getSubscription();
  if (sub && Notification.permission === "granted") { send("/api/push/subscribe", "POST", sub.toJSON()).catch(() => {}); return "on"; }
  return Notification.permission === "denied" ? "denied" : "off";
}
async function turnOnAlerts(){
  const perm = await Notification.requestPermission();
  if (perm !== "granted") throw new Error("Alerts are blocked for this site. Allow notifications for it in the browser or phone settings, then reload.");
  const reg = await navigator.serviceWorker.register("/sw.js");
  const {key} = await api("/api/push/key");
  const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64ToBytes(key)});
  await send("/api/push/subscribe", "POST", sub.toJSON());
  await send("/api/push/test", "POST");
}
function drawApp(body){
  const cards = el("div", "cards"); body.append(cards);
  const al = el("section", "card brk w6"); al.append(el("h2", null, "Alerts on this device"));
  const txt = el("p", "dim", "Checking\u2026"), acts = el("div", "acts");
  al.append(txt, acts); cards.append(al);
  const paint = st => {
    acts.replaceChildren();
    const ios = /iPhone|iPad/.test(navigator.userAgent) && !standalone();
    if (st === "on") { txt.textContent = "On. Assessments, interviews, codes, approvals and the morning list come to this device."; acts.append(button("Send a test alert", () => send("/api/push/test", "POST").then(() => toast("Test alert on its way"), fail), "sm")); }
    else if (ios) txt.textContent = "On iPhone, alerts work only in the home-screen app: tap Share, then Add to Home Screen, open it from there and turn alerts on.";
    else if (st === "unsupported") txt.textContent = "This browser can't show alerts from the panel.";
    else if (st === "denied") txt.textContent = "Alerts are blocked for this site. Allow notifications for it in the browser or phone settings, then reload.";
    else { txt.textContent = "Off. Turn them on to hear about assessments, interviews and offers the moment they arrive."; acts.append(button("Turn on alerts", () => turnOnAlerts().then(() => { toast("Alerts are on"); paint("on"); }, fail), "go")); }
  };
  alertState().then(paint, () => paint("unsupported"));
  const af = el("section", "card brk w6"); af.append(el("h2", null, "Autofill script"), el("p", "dim", "Fills application forms in your browser and submits the ones you approved. Install it in Tampermonkey or Violentmonkey (Userscripts on iPhone); it updates itself after that."));
  const a1 = el("div", "acts"); const inst = el("a", "btn go", "Install or update"); inst.href = "/jobs-fill.user.js"; a1.append(inst); af.append(a1); cards.append(af);
  const cl = el("section", "card brk w12"); cl.append(el("h2", null, "Classic panel"), el("p", "dim", "The previous design, kept for now. Everything it does is also here."));
  const a2 = el("div", "acts"); a2.append(link("/classic", "Open the classic panel")); cl.append(a2); cards.append(cl);
}

// ---------- home (dashboard) ----------
let counted = false;
function drawDash(){
  const box = $("dash"); box.replaceChildren();
  const s = D.jobs, rep = D.report && D.report.report;
  const review = s ? (s.review || []).length : 0;

  const hero = el("div", "dhero"), l = el("div");
  l.append(el("h1", null, greeting() + "  //  " + new Date().toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"}).toUpperCase()));
  const p = el("p");
  if (!s) p.textContent = "Loading\u2026";
  else if (!s.configured) p.textContent = "The job search isn't set up yet: put config.json and resume.txt in the jobs folder on the server.";
  else if (review) { p.append(el("b", null, review + (review === 1 ? " application is" : " applications are"))); p.append(" ready for you to review."); }
  else p.textContent = "Nothing to review right now. " + (s.not_ready ? s.not_ready + " more are being prepared." : "New ones are prepared overnight.");
  l.append(p); hero.append(l);
  if (review) hero.append(button("Start reviewing \u25B8", startReview, "go", "Enter"));
  box.append(hero);

  const cards = el("div", "cards");
  box.append(cards);

  // 1. needs your attention: emails that need him, codes, deadlines, and the agent's approvals
  const pri = el("section", "card w8 brk"), need = needsYou();
  const total = need.length + (AG.pending ? 1 : 0);
  pri.classList.toggle("warn", total > 0);
  const h = el("h2"); h.append(el("span", null, "Needs your attention"), total ? chip("warn", total + (total === 1 ? " item" : " items")) : chip("good", "All clear"));
  pri.append(h);
  if (AG.pending) {
    const row = el("div", "trow");
    row.append(chip("warn", "Approval"), el("span", "tt", "The assistant wants to run " + AG.pending.tool), el("span", "dim", "Review \u25B8"), el("span", "tm", AG.task || ""));
    row.onclick = () => $("approval").click();
    pri.append(row);
  }
  if (!D.inbox) pri.append(el("div", "dim", "Loading\u2026"));
  else if (!D.inbox.configured) { const row = el("div", "trow"); row.append(chip("", "Setup"), el("span", "tt", "Connect the agent's inbox to track replies"), el("span", "dim", "Connect \u25B8")); row.onclick = () => go("inbox"); pri.append(row); }
  else if (!total) pri.append(el("div", "dim", "Nothing needs you. Assessments, interview requests and sign-in codes appear here the moment they land."));
  for (const m of need.slice(0, 6)) {
    const k = MAIL[m.kind] || ["", m.kind], row = el("div", "trow"), due = dueAt(m.due);
    const right = m.code ? el("span", "code", m.code) : due ? countdown(due) : el("span", "dim", m.due || ago(m.date));
    if (m.code) right.style.fontSize = "16px";
    row.append(chip(k[0], k[1]), el("span", "tt", who(m) + (m.title ? "  \u00b7  " + m.title : "")), right,
      el("span", "tm", [m.when, m.summary].filter(Boolean).join("  \u00b7  ")));
    row.onclick = () => jumpTo("inbox", "needs", "m" + m.uid);
    pri.append(row);
  }
  cards.append(pri);

  // 2. ready to review
  const mis = el("section", "card brk");
  mis.append(el("h2", null, "Ready to review"));
  const br = el("div", "bigrow"), big = el("span", "big", s ? String(review) : "--");
  big.dataset.n = review;
  br.append(big, el("span", "what", review === 1 ? "application" : "applications"));
  mis.append(br);
  if (s) {
    mis.append(line("Being prepared", s.not_ready), line("Approved, waiting to send", s.approved), line("Agency jobs ready", (s.agency_ready || []).length));
    const acts = el("div", "acts");
    if (review) acts.append(button("Start reviewing \u25B8", startReview, "go"));
    const ap = applyButton(); if (ap) acts.append(ap);
    if (!review && !ap) acts.append(button("See all jobs", () => go("jobs")));
    mis.append(acts);
  }
  cards.append(mis);

  // 3. the last 7 days
  const fr = el("section", "card w8 brk");
  const fh = el("h2"); fh.append(el("span", null, "Last 7 days"));
  fr.append(fh);
  if (rep && rep.found && rep.found.days) {
    const days = rep.found.days, max = Math.max(1, ...days.map(d => d.found));
    fh.append(chip("", rep.found.total + " found"));
    const bars = el("div", "bars"), labels = el("div", "bdays");
    for (const d of days) {
      const b = el("div", "bar"), rest = el("i"), strong = el("i", "s");
      rest.style.height = ((d.found - d.strong) / max * 100) + "%"; strong.style.height = (d.strong / max * 100) + "%";
      b.title = d.day + ": " + d.found + " found, " + d.good + " good, " + d.strong + " strong";
      if (d.found) b.append(el("em", null, String(d.found)));
      b.append(rest, strong); bars.append(b);
      labels.append(el("span", null, new Date(d.day + "T12:00").toLocaleDateString([], {weekday: "short"}).toUpperCase()));
    }
    const lg = el("div", "legend"), a = el("span"), b = el("span");
    a.append(el("i"), "jobs found"); b.append(el("i", "s"), "strong matches");
    lg.append(a, b);
    fr.append(bars, labels, lg);
    const f = rep.funnel && rep.funnel.week;
    if (f) {
      const st = el("div", "stats"); st.style.marginTop = "14px";
      for (const [k, v] of [["Applied", f.applied], ["Replies", f.replied], ["Interviews", f.interview], ["Rejected", f.rejected], ["Offers", f.offer]]) { const d = el("div"); d.append(el("b", null, String(v || 0)), el("span", null, k)); st.append(d); }
      fr.append(st);
    }
    fr.style.cursor = "pointer"; fr.onclick = e => { if (!e.target.closest("button, a")) go("insights"); };
  } else fr.append(el("div", "dim", D.report ? "No report yet. It's built from the nightly searches." : "Loading\u2026"));
  cards.append(fr);

  // 4. the nightly search
  const sv = el("section", "card brk");
  sv.append(el("h2", null, "Nightly job search"));
  if (s) {
    const pr = s.progress || {}, lr = s.last_run || {};
    if (pr.running) {
      const bar = el("div", "pbar"), i = el("i"); i.style.width = (pr.total ? pr.done / pr.total * 100 : 4) + "%"; bar.append(i);
      sv.append(el("div", null, "Searching now: " + pr.step), bar, el("div", "dim", pr.total ? pr.done + " / " + pr.total : ""));
    } else sv.append(el("div", "dim", lr.finished ? "Last search " + ago(lr.finished) + " (" + when(lr.finished) + ")" : "No search yet."));
    if (lr.boards != null) {
      sv.append(line("Job boards checked", lr.boards), line("Postings scanned", (lr.fetched || 0).toLocaleString()), line("New postings", lr.new), line("Good matches", lr.matched));
      if ((lr.board_errors || []).length) { const e = line("Boards that failed", lr.board_errors.length); e.title = lr.board_errors.join("\n"); e.querySelector("b").style.color = "var(--amber)"; sv.append(e); }
    }
    const sb = searchButton();
    if (sb) { const a = el("div", "acts"); a.append(sb); sv.append(a); }
  } else sv.append(el("div", "dim", "Loading\u2026"));
  cards.append(sv);

  // 5. the week's top suggestion
  const sug = rep && (rep.suggestions || [])[0];
  if (sug) {
    const c = el("section", "card w12 brk tip"), ih = el("h2");
    ih.append(el("span", null, "Top suggestion this week"), chip("", "1 of " + rep.suggestions.length));
    c.append(ih, el("b", null, sug.title), el("p", null, sug.detail));
    if (sug.evidence) c.append(el("p", "faint", sug.evidence));
    c.style.cursor = "pointer"; c.onclick = () => go("insights");
    cards.append(c);
  }
  if (!counted && D.jobs && !REDUCED) { counted = true; countUp(box); }
}
function line(k, v){ const d = el("div", "line"); d.append(el("span", null, k), el("b", null, v == null ? "--" : String(v))); return d; }
function countUp(root){
  for (const n of root.querySelectorAll(".big[data-n]")) {
    const to = +n.dataset.n; if (!to) continue;
    const t0 = performance.now();
    const f = t => { const k = Math.min(1, (t - t0) / 700); n.textContent = String(Math.round(to * (1 - Math.pow(1 - k, 3)))); if (k < 1) requestAnimationFrame(f); };
    requestAnimationFrame(f);
  }
}
function greeting(){ const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }
function startReview(){ TAB.jobs = "awaiting"; FILTER.jobs = ""; go("jobs"); if (phone() && curItem()) openDetail(); }

// ---------- status bar and clock ----------
function drawStatus(){
  const box = $("status"); box.replaceChildren();
  const pr = D.jobs && D.jobs.progress;
  if (!linkOk) { box.append(el("i", "led bad"), el("span", null, "Offline")); box.title = linkErr; return; }
  box.title = "";
  if (pr && pr.running) { box.append(el("i", "led busy"), el("span", null, "Searching " + (pr.total ? pr.done + "/" + pr.total : "now"))); return; }
  box.append(el("i", "led"), el("span", null, "Connected"));
}
function tick(){
  const d = new Date();
  $("clock").textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  for (const e of document.querySelectorAll(".tminus[data-due]")) tickOne(e);
}

// ---------- routing ----------
function go(p){
  p = OLD[p] || p;
  if (!PLACE[p]) p = "home";
  if (place !== p) {
    if (p === "apps") { appsMarkSince = appsSeen; appsSeen = Date.now(); store("v2:appsSeen", String(appsSeen)); }  // opening Applications clears its badge
    place = p; listSig = ""; actsSig = ""; shownKey = ""; editing = false; pageBuilt = ""; closeDetail();
  }
  if (location.hash !== "#" + p) history.replaceState(null, "", "#" + p);
  $("q").value = FILTER[p] || "";
  $("q").placeholder = PLACE[p].find || "Search";
  render();
  if (LISTS[p]) $("list").scrollTop = 0;
  drawAgentStatus();
}
function render(){
  const main = $("main");
  main.classList.toggle("dash", place === "home");
  main.classList.toggle("solo", !!PAGES[place]);
  if (place === "home") drawDash();
  else if (PAGES[place]) drawPage(false);
  else drawList();
  drawNav(); drawStatus();
}

// ---------- keys ----------
// Single keys, Gmail style: j/k move, Enter opens, a/s approve or skip, o opens the link,
// / or ⌘K searches, g then a letter jumps to a place, 1-4 switch tabs, ? lists them all.
let gPending = 0;
function focusFilter(){
  if (!LISTS[place]) go("jobs");
  const q = $("q"); q.focus(); q.select();
}
function press(key){ const b = $("detail").querySelector("[data-key='" + key + "']"); if (b && !b.disabled) { b.click(); return true; } return false; }
document.addEventListener("keydown", e => {
  const inField = e.target.matches("input, textarea, select, [contenteditable]");
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); focusFilter(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (!$("help").hidden) { if (e.key === "Escape" || e.key === "?") { e.preventDefault(); $("help").hidden = true; } return; }
  if (inField) {
    if (e.target.id !== "q") { if (e.key === "Escape") e.target.blur(); return; }
    if (e.key === "Escape") { e.preventDefault(); if (e.target.value) { e.target.value = ""; FILTER[place] = ""; render(); } e.target.blur(); }
    else if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); e.target.blur(); if (e.key === "Enter" && phone() && curItem()) openDetail(); }
    return;
  }
  const k = e.key;
  if (gPending && Date.now() - gPending < 1200) {
    gPending = 0;
    const p = PLACES.find(x => x.key === k.toLowerCase());
    if (p) { e.preventDefault(); go(p.id); }
    return;
  }
  if (k === "g") { gPending = Date.now(); return; }
  if (k === "?") { e.preventDefault(); showHelp(); return; }
  if (k === "/") { e.preventDefault(); focusFilter(); return; }
  if (place === "home") {
    if (k === "Enter" && D.jobs && (D.jobs.review || []).length) { e.preventDefault(); startReview(); }
    return;
  }
  if (!LISTS[place]) return;
  if (k === "j" || k === "ArrowDown") { e.preventDefault(); move(1); }
  else if (k === "k" || k === "ArrowUp") { e.preventDefault(); move(-1); }
  else if (k === "Enter") { if (curItem()) { e.preventDefault(); openDetail(); } }
  else if (k === "Escape") { if ($("main").classList.contains("open")) closeDetail(); else if (FILTER[place]) { FILTER[place] = ""; $("q").value = ""; render(); } }
  else if (k === "a" || k === "s" || k === "d") { if (press(k)) e.preventDefault(); }
  else if (k === "o") { const a = $("detail").querySelector("a.btn[href^='https://']"); if (a) { e.preventDefault(); window.open(a.href, "_blank", "noopener,noreferrer"); } }
  else if (k === "c") { const it = curItem(); if (it && it.obj.code) copy(it.obj.code, "Code"); }
  else if (/^[1-9]$/.test(k)) { const t = LISTS[place].tabs[+k - 1]; if (t) { e.preventDefault(); setTab(t[0]); } }
});
$("q").addEventListener("input", e => { FILTER[place] = e.target.value; render(); });
$("kbtn").onclick = focusFilter;
$("qicon").append(icon("search"));
$("approval").prepend(icon("warn"));
window.addEventListener("hashchange", () => go(location.hash.slice(1)));
let wasPhone = phone();
window.addEventListener("resize", () => { if (phone() !== wasPhone) { wasPhone = phone(); closeDetail(); } });
// Swipe on the phone while reviewing: right to approve, left to skip
(() => {
  let x0 = 0, y0 = 0;
  const d = $("detail");
  d.addEventListener("touchstart", e => { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, {passive: true});
  d.addEventListener("touchend", e => {
    if (!LISTS[place] || e.target.closest("input, textarea, select, .fold")) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    if (Math.abs(dx) > 90 && Math.abs(dy) < 50) press(dx > 0 ? "a" : "s");
  }, {passive: true});
})();

function showMore(){
  const o = $("help"); o.replaceChildren();
  const s = el("div", "sheet brk"); s.append(el("h1", null, "More"));
  for (const p of PLACES.filter(x => x.more)) {
    const b = el("button", "nav"); const ic = el("span", "ic"); ic.append(icon(p.id));
    b.append(ic, el("span", null, p.label + (p.id === "assistant" && AG.pending ? "  \u00b7  approval waiting" : ""))); b.style.width = "100%"; b.style.marginTop = "8px";
    b.onclick = () => { o.hidden = true; go(p.id); };
    s.append(b);
  }
  o.append(s); o.hidden = false;
  o.onclick = e => { if (e.target === o) o.hidden = true; };
}
function showHelp(){
  const o = $("help"); o.replaceChildren();
  const s = el("div", "sheet brk"); s.append(el("h1", null, "Keyboard shortcuts"), el("p", "dim", "Optional: everything also works by clicking or tapping. On the phone, swipe right to approve and left to skip."));
  const g = el("div", "keys");
  const rows = [["\u2191 \u2193  or  j k", "Move down / up the list"], ["Enter", "Open (on Home: start reviewing)"], ["a", "Approve the application"], ["s", "Skip it"],
    ["Esc", "Go back / clear the search"], ["o", "Open the job posting or the email"], ["d", "Mark an email done"], ["c", "Copy a sign-in code"],
    ["/  or  \u2318K", "Search the list"], ["1\u20134", "Switch tabs"]]
    .concat(PLACES.map(p => ["g " + p.key, "Go to " + p.label])).concat([["?", "This list"]]);
  for (const [a, b] of rows) g.append(el("kbd", "key", a), el("span", "dim", b));
  s.append(g); o.append(s); o.hidden = false;
  o.onclick = e => { if (e.target === o) o.hidden = true; };
}

// ---------- boot ----------
// Once per browser session: a short boot log (any key or tap skips it). Afterwards the
// top bar types one status line.
function boot(){
  const counts = [["connecting", "OK", "ok"],
    ["loading jobs", D.jobs ? String(D.jobs.jobs.length) : "OK", "ok"],
    ["loading inbox", D.inbox ? String((D.inbox.messages || []).length) : "OK", "ok"],
    ["needs your attention", String(needsYou().length), needsYou().length ? "warn" : "ok"]];
  let skip = REDUCED;
  try { skip = skip || sessionStorage.getItem("v2:booted"); sessionStorage.setItem("v2:booted", "1"); } catch (e) {}
  typeBootline();
  if (skip) return;
  const b = $("boot"), log = $("bootlog"); b.classList.add("on");
  log.append(el("div", null, "JOB // AGENT"), el("div", "faint", "\u2500".repeat(44)));
  let i = 0, done = false;
  const finish = () => { if (done) return; done = true; b.classList.add("out"); setTimeout(() => b.classList.remove("on", "out"), 350); removeEventListener("keydown", finish, true); };
  addEventListener("keydown", finish, true); b.onclick = finish;
  const next = () => {
    if (done) return;
    if (i < counts.length) {
      const [what, val, cls] = counts[i++], row = el("div");
      row.append("> " + what + " " + ".".repeat(Math.max(3, 34 - what.length)) + " ", el("span", cls, val));
      log.append(row); setTimeout(next, 120);
    } else { log.append(el("div", "ok", "> ready")); setTimeout(finish, 300); }
  };
  setTimeout(next, 160);
}
function typeBootline(){
  const n = D.jobs ? D.jobs.jobs.length : 0;
  const text = "> connected  \u00b7  " + (n ? n + " jobs tracked  \u00b7  " : "") + "press ? for keyboard shortcuts";
  const box = $("bootline");
  if (REDUCED) { box.textContent = text; return; }
  let i = 0; const step = () => { box.textContent = text.slice(0, ++i); if (i < text.length) setTimeout(step, 18); };
  step();
}

// ---------- reticle cursor ----------
// With a mouse, the pointer is a targeting reticle: a crosshair that glides after the
// mouse, and whose four corner brackets snap around the button, row or tab under it.
function reticle(){
  if (REDUCED || !matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  document.documentElement.classList.add("reticle");
  const r = $("reticle"), TARGET = "button, a, .row, .trow, .tab, .citem, .card[style*=pointer], input, select, textarea, label.filter, label.consent, summary";
  let x = -100, y = -100, cx = -100, cy = -100, lock = null, box = null, raf = 0;
  const frame = () => {
    raf = 0;
    cx += (x - cx) * .35; cy += (y - cy) * .35;
    if (lock && lock.isConnected) {
      const b = lock.getBoundingClientRect();
      box = {l: b.left - 4, t: b.top - 4, w: b.width + 8, h: b.height + 8};
    } else box = null;
    const s = r.style;
    if (box) { s.transform = "translate(" + box.l + "px," + box.t + "px)"; s.width = box.w + "px"; s.height = box.h + "px"; }
    else { s.transform = "translate(" + (cx - 14) + "px," + (cy - 14) + "px)"; s.width = s.height = "28px"; }
    r.classList.toggle("lock", !!box);
    r.querySelector("b").style.transform = "translate(" + (x - (box ? box.l : cx - 14)) + "px," + (y - (box ? box.t : cy - 14)) + "px)";
    if (Math.abs(x - cx) > .5 || Math.abs(y - cy) > .5) raf = requestAnimationFrame(frame);
  };
  const kick = () => { if (!raf) raf = requestAnimationFrame(frame); };
  addEventListener("mousemove", e => {
    x = e.clientX; y = e.clientY; r.classList.add("on");
    const t = e.target.closest && e.target.closest(TARGET);
    lock = t && t.offsetWidth < innerWidth * .7 && t.offsetHeight < innerHeight * .5 ? t : null;  // a whole panel is too big to lock onto
    kick();
  }, {passive: true});
  addEventListener("scroll", kick, {passive: true, capture: true});
  document.addEventListener("mouseleave", () => r.classList.remove("on"));
  addEventListener("mousedown", () => { r.classList.add("fire"); setTimeout(() => r.classList.remove("fire"), 160); });
}

// A tapped notification about jobs or the inbox brings the open panel to Jobs
if (navigator.serviceWorker) navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.view === "jobs") go("jobs"); });

buildNav();
reticle();
go(location.hash.slice(1) || "home");
boot();
pollAll();
setInterval(tick, 1000); tick();

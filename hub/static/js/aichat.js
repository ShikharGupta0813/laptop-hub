// "AI for this chat": the ONE control that decides which AI answers the Talk tab, plus the explicit "Send to lane" picker.
//   const choices = await loadChoices();                  -> [{id, label, sub, ready, reason, group}]
//   const btn = aiButton({ value, onChange, ctx });       -> a compact button showing the current AI; tap opens the list
//   const laneId = await chooseLane({ ctx });             -> the lane to hand ONE message to (or null), a separate action
// Choice ids: "auto" | "local" | "claude" | "claude-login" | "gemini" | "duo" | "cli:<id>" (Antigravity...) are real providers
// (the server's `provider` field); "lane:council" and "lane:local-reasoning" answer through a work lane (hub/lanes.py) and are
// posted into the chat like any other reply. Every string is shown with textContent (el()), never as HTML.
import { el } from "./api.js";
import * as ui from "./ui.js";
import { loadProviders, providerChoices, providerLabel, isProvider } from "./providers.js";
import { loadLanes, forgetLanes } from "./lanepicker.js";

const CSS = "/css/aichat.css";
const css = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };

export const isLaneChoice = (id) => typeof id === "string" && id.startsWith("lane:");
export const laneOf = (id) => (isLaneChoice(id) ? id.slice(5) : "");
export const isChoice = (id) => id === "auto" || isLaneChoice(id) || isProvider(id);
export const normalizeChoice = (id, fallback = "local") => (isChoice(id) ? id : fallback);

const SUBS = {
  auto: "Picks for each message: quick questions stay on this laptop, heavy ones go to the strongest AI that is ready.",
};
export const COUNCIL_OFF = "Council isn't set up yet";

// [{id, label, sub, ready, reason, group}] in the order the sheet shows them. Not-ready ones are kept (greyed, with the reason).
export async function loadChoices(force = false) {
  const [, lanes] = await Promise.all([loadProviders(force), loadLanes(force).catch(() => null)]);
  const out = [{ id: "auto", label: "Auto", sub: SUBS.auto, ready: true, reason: "", group: "ai" }];
  for (const p of providerChoices()) out.push({ id: p.id, label: p.label, sub: p.ready ? p.reason : "", ready: p.ready, reason: p.ready ? "" : p.reason, group: "ai" });
  const lane = (id) => (lanes?.lanes || []).find((l) => l.id === id);
  const deep = lane("local-reasoning");
  if (deep) {
    const ok = deep.enabled !== false && !deep.why_not;
    out.push({ id: "lane:local-reasoning", label: "Local deep thinking", sub: ok ? "Slow, free and private: the deep-thinking model on this laptop." : "",
      ready: ok, reason: ok ? "" : (deep.why_not || "Switched off."), group: "lane" });
  }
  const council = lane("council");
  if (council) {
    const ok = council.enabled !== false && lanes?.council?.ready !== false && !council.why_not;
    out.push({ id: "lane:council", label: "Council", sub: ok ? "Your models working together in the background. Slow, free; the answer lands here when it is done." : "",
      ready: ok, reason: ok ? "" : COUNCIL_OFF, group: "lane", setup: !ok });
  }
  return out;
}

export function choiceLabel(id) {
  return providerLabel(id);
}

// How to set the Council up (a sheet, never a failed run).
export function councilSetupSheet(ctx) {
  css();
  ui.sheet({
    title: "Set up the Council",
    content: el("div", { class: "ui-stack ac-help" },
      el("p", {}, "Council runs your multi-llm-lab project: several models working on one task in the background. It needs the lab folder on the laptop."),
      el("p", {}, "On the laptop, run this once, then restart the hub:"),
      el("pre", { class: "ac-cmd" }, "python -m hub setup --only council"),
      el("p", { class: "ui-hint" }, "Until then, pick another AI for this chat. Nothing is sent anywhere by trying.")),
    actions: [
      ...(ctx?.open ? [{ label: "Open Council", variant: "primary", icon: "users", onClick: () => { ctx.open("council"); } }] : []),
      { label: "Close", variant: "ghost" },
    ],
  });
}

function pickerRows({ choices, current, onPick, ctx, close }) {
  const rows = choices.map((c) => {
    const on = c.id === current;
    const row = el("button", { type: "button", class: "ac-row" + (c.ready ? "" : " is-off"), role: "radio", "aria-checked": String(on),
      "aria-disabled": c.ready ? "false" : "true", "data-choice": c.id },
      el("span", { class: "ac-row__body" },
        el("span", { class: "ac-row__name" }, c.label),
        (c.ready ? c.sub : c.reason) ? el("span", { class: "ac-row__sub" }, c.ready ? c.sub : c.reason) : null),
      c.ready && on ? ui.icon("check") : null,
      !c.ready ? el("span", { class: "ac-row__tag" }, "not ready") : null);
    row.addEventListener("click", () => { if (!c.ready) return; onPick(c.id); close(); });
    if (c.setup) {
      const how = ui.button({ label: "How to set up", icon: "help", variant: "tonal", size: "sm", class: "ac-how", onClick: () => councilSetupSheet(ctx) });
      return el("div", { class: "ac-rowwrap" }, row, how);
    }
    return row;
  });
  return rows;
}

// The compact control. Shows the current AI by name, truncated with an ellipsis, readable at 320px.
export function aiButton({ value, onChange, ctx } = {}) {
  css();
  let current = value;
  let choices = null;
  const name = el("span", { class: "ac-btn__name" }, choiceLabel(current));
  const btn = el("button", { type: "button", class: "ac-btn", "aria-haspopup": "dialog", "data-ai": current },
    ui.icon("sparkles"),
    el("span", { class: "ac-btn__text" }, el("span", { class: "ac-btn__cap" }, "AI for this chat"), name),
    ui.icon("chevron-down", { class: "ac-btn__chev" }));
  const paint = () => {
    const label = choices?.find((c) => c.id === current)?.label || choiceLabel(current);
    name.textContent = label;
    btn.dataset.ai = current;
    btn.setAttribute("aria-label", `AI for this chat: ${label}. Tap to change.`);
    btn.title = label;
  };
  paint();
  loadChoices().then((c) => { choices = c; paint(); }).catch(() => {});

  btn.addEventListener("click", async () => {
    forgetLanes();
    try { choices = await loadChoices(true); } catch (_) { /* keep the last list */ }
    if (!choices) choices = await loadChoices();
    const list = el("div", { class: "ac-list", role: "radiogroup", "aria-label": "AI for this chat" });
    let sh;
    const rows = pickerRows({ choices, current, ctx, close: () => sh.close(), onPick: (id) => { current = id; paint(); onChange?.(id); } });
    const ai = [], lane = [];
    choices.forEach((c, i) => (c.group === "lane" ? lane : ai).push(rows[i]));
    list.append(...ai);
    if (lane.length) list.append(el("p", { class: "ui-caps ac-caps" }, "Slower, in the background"), ...lane);
    sh = ui.sheet({
      title: "AI for this chat",
      content: el("div", { class: "ui-stack" },
        el("p", { class: "ui-hint ac-lead" }, "Which AI answers the next messages in this chat. Your choice is remembered for this conversation, and each reply says which AI wrote it."),
        list),
      actions: [{ label: "Close", variant: "ghost" }],
    });
  });
  return { el: btn, get value() { return current; }, set value(v) { current = v; paint(); }, refresh() { return loadChoices(true).then((c) => { choices = c; paint(); }); } };
}

// "Send to lane": an explicit one-off hand-over of the written message to a work lane. Resolves with a lane id or null.
// (It does not change the AI for this chat. The caller posts the result into the chat.)
export async function chooseLane({ ctx } = {}) {
  css();
  forgetLanes();
  let data = null;
  try { data = await loadLanes(true); } catch (_) { /* shown below */ }
  return new Promise((resolve) => {
    let picked = null;
    const lanes = (data?.lanes || []).filter((l) => l.enabled !== false);
    const body = el("div", { class: "ac-list", role: "list" });
    let sh;
    if (!lanes.length) body.append(el("p", { class: "ui-hint" }, data ? "No work lanes are switched on. Turn some on in Settings > Work lanes." : "Couldn't load the work lanes. Check the connection and try again."));
    for (const l of lanes) {
      const ok = !l.why_not && (l.id !== "council" || data?.council?.ready !== false);
      const row = el("button", { type: "button", class: "ac-row" + (ok ? "" : " is-off"), "aria-disabled": ok ? "false" : "true" },
        el("span", { class: "ac-row__body" },
          el("span", { class: "ac-row__name" }, l.label.replace(/ \(.*\)$/, "")),
          el("span", { class: "ac-row__sub" }, ok ? `${l.cost || ""}${l.notes ? " · " + l.notes : ""}`.replace(/^ · /, "") : (l.id === "council" ? COUNCIL_OFF : l.why_not))));
      row.addEventListener("click", () => { if (!ok) return; picked = l.id; sh.close(); });
      body.append(l.id === "council" && !ok
        ? el("div", { class: "ac-rowwrap" }, row, ui.button({ label: "How to set up", icon: "help", variant: "tonal", size: "sm", class: "ac-how", onClick: () => councilSetupSheet(ctx) }))
        : row);
    }
    sh = ui.sheet({
      title: "Send to lane",
      content: el("div", { class: "ui-stack" },
        el("p", { class: "ui-hint ac-lead" }, "Hands this one message to a work lane. The answer is posted here in the chat. Nothing else changes."), body),
      actions: [{ label: "Cancel", variant: "ghost" }],
      onClose: () => resolve(picked),
    });
  });
}

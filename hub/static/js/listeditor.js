// List editor: the one ARC-Glass look for every list the owner edits in Settings (assistants, notification
// preferences, safety rules, redaction rules, memory kinds, ...). Small on purpose, and every string goes in with
// textContent only, so a rule or a name can never become markup.
//
//   const ed = listEditor({
//     items, rowOf: (item) => ({ icon, tone, title, sub, meta, enabled }),
//     onToggle: async (item, on) => {...}, onEdit: (item) => {...}, onDelete: async (item) => {...},
//     onAdd: () => {...}, onReset: async () => {...}, addLabel, resetLabel, empty, canDelete: (item, all) => bool });
//   container.append(ed.el); ed.set(newItems);
//
// rowOf().enabled (true/false) shows a switch; leave it undefined for a list without on/off.
// formSheet() is the matching add/edit form: fields of type text, textarea, select, number, switch, chips (a list of
// words) and note (read-only text).
import { el } from "./api.js";
import * as ui from "./ui.js";

const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });

export function listEditor({ title, hint, items = [], rowOf, onToggle, onEdit, onDelete, onAdd, onReset, addLabel = "Add",
  resetLabel = "Reset to defaults", empty = "Nothing here yet.", canDelete } = {}) {
  let current = items;
  const list = el("div", { class: "ui-list ui-list--inset ui-list--icons le-list" });
  const foot = el("div", { class: "le-foot" });
  const root = el("section", { class: "le st-group" },
    title ? el("h2", { class: "ui-caps st-caps" }, title) : null,
    hint ? el("p", { class: "ui-hint le-hint" }, hint) : null, list, foot);

  function row(item) {
    const r = rowOf(item) || {};
    const node = el("div", { class: "ui-row le-row" + (r.enabled === false ? " is-off" : "") },
      r.icon ? ui.tile(r.icon, r.tone || "accent") : null,
      el("span", { class: "ui-row__body" },
        el("span", { class: "ui-row__title" }, r.title ?? ""),
        r.sub ? el("span", { class: "ui-row__sub" }, r.sub) : null));
    if (r.meta != null && r.meta !== "") node.append(el("span", { class: "ui-row__meta le-meta" }, r.meta));
    const actions = el("span", { class: "le-actions" });
    if (typeof r.enabled === "boolean" && onToggle) {
      const sw = ui.switchControl({ checked: r.enabled, label: `${r.title || "Item"} on or off`,
        onChange: async (on) => { try { await onToggle(item, on); } catch (error) { sw.input.checked = !on; fail(error); } } });
      actions.append(sw);
    }
    if (onEdit) actions.append(ui.button({ label: `Edit ${r.title || ""}`.trim(), icon: "edit", variant: "ghost", size: "sm", iconOnly: true, onClick: () => onEdit(item) }));
    if (onDelete && (!canDelete || canDelete(item, current))) {
      actions.append(ui.button({ label: `Delete ${r.title || ""}`.trim(), icon: "trash", variant: "ghost", size: "sm", iconOnly: true, onClick: async () => {
        if (!(await ui.confirm({ title: "Delete this?", message: String(r.title || ""), confirmLabel: "Delete", danger: true, icon: "trash" }))) return;
        try { await onDelete(item); } catch (error) { fail(error); }
      } }));
    }
    node.append(actions);
    return node;
  }

  function paint() {
    list.replaceChildren(...(current.length ? current.map(row) : [el("p", { class: "ui-hint le-empty" }, empty)]));
    foot.replaceChildren(
      onAdd ? ui.button({ label: addLabel, icon: "plus", variant: "tonal", size: "sm", onClick: () => onAdd() }) : null,
      onReset ? ui.button({ label: resetLabel, icon: "refresh", variant: "ghost", size: "sm", onClick: async () => {
        if (!(await ui.confirm({ title: resetLabel + "?", message: "Your changes to this list are removed.", confirmLabel: "Reset", danger: true, icon: "refresh" }))) return;
        try { await onReset(); } catch (error) { fail(error); }
      } }) : null);
  }
  paint();
  return { el: root, set(next) { current = Array.isArray(next) ? next : []; paint(); }, get items() { return current; } };
}

// formSheet({title, fields, values, onSave(values) -> false keeps it open, saveLabel, extra})
// fields: [{key, label, type, options, hint, placeholder, maxlength, rows, min, max, step}]
export function formSheet({ title, fields, values = {}, onSave, saveLabel = "Save", extra = null, tall = false } = {}) {
  const inputs = {};
  const nodes = fields.map((f) => {
    const v = values[f.key];
    let input;
    if (f.type === "note") return el("p", { class: "ui-hint" }, f.text || f.label || "");
    if (f.type === "textarea") {
      input = el("textarea", { class: "ui-input", rows: String(f.rows || 4), maxlength: String(f.maxlength || 2000), placeholder: f.placeholder || "" });
      input.value = v ?? "";
    } else if (f.type === "select") {
      input = el("select", { class: "ui-input" }, ...(f.options || []).map(([value, label]) => el("option", { value }, label)));
      input.value = v ?? (f.options?.[0]?.[0] ?? "");
    } else if (f.type === "switch") {
      input = ui.switchControl({ checked: v !== false && !!(v ?? f.default ?? true), label: f.label });
    } else if (f.type === "chips") {
      input = el("input", { class: "ui-input", type: "text", maxlength: String(f.maxlength || 400), placeholder: f.placeholder || "word, another word" });
      input.value = Array.isArray(v) ? v.join(", ") : (v ?? "");
    } else {
      input = el("input", { class: "ui-input", type: f.type === "number" ? "number" : "text", maxlength: String(f.maxlength || 200), placeholder: f.placeholder || "",
        ...(f.type === "number" ? { min: f.min, max: f.max, step: f.step || "any" } : {}) });
      input.value = v ?? "";
    }
    if (f.readonly) input.disabled = true;
    inputs[f.key] = { input, type: f.type };
    if (f.type === "switch") {
      return el("div", { class: "ui-row le-switchfield" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, f.label),
        f.hint ? el("span", { class: "ui-row__sub" }, f.hint) : null), input);
    }
    return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, f.label), input, f.hint ? el("span", { class: "ui-hint" }, f.hint) : null);
  });
  const read = () => {
    const out = {};
    for (const [key, { input, type }] of Object.entries(inputs)) {
      if (type === "switch") out[key] = input.input.checked;
      else if (type === "number") out[key] = input.value === "" ? null : Number(input.value);
      else if (type === "chips") out[key] = input.value.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
      else out[key] = input.value;
    }
    return out;
  };
  return ui.sheet({ title, class: "st-sheet" + (tall ? " st-sheet--tall" : ""), content: el("div", { class: "ui-stack ui-stack--lg" }, ...nodes, extra),
    actions: [{ label: saveLabel, variant: "primary", icon: "check", onClick: async () => {
      try { return (await onSave(read())) === false ? false : true; } catch (error) { fail(error); return false; }
    } }] });
}

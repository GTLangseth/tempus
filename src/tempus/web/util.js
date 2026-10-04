// Small helpers shared by the views.
export const FREQ = { D: "daily", W: "weekly", M: "monthly", Q: "quarterly", Y: "annual" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const fmtInt = (n) => (n ?? 0).toLocaleString("en-US");

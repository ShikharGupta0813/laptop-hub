// One place for "something optional is not installed" messages. The installer script installs everything at once, so
// every screen points at it instead of at a pip command (Python twin: hub/common.py setup_all_hint).
export const SETUP_ALL = "scripts\\setup-all.ps1";
export const setupHint = (what = "") => `${what ? what + " needs extra packages. " : ""}Run ${SETUP_ALL} (installs everything).`;

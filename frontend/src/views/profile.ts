// src/views/profile.ts
//
// Profile page: read-only account info + change-own-password form.

import { getMe, changeOwnPassword, type MeInfo } from "../api";
import { updateStoredPassword } from "../auth";
import { escapeHtml } from "./_util";

const INPUT_STYLE = [
  "width:100%",
  "background:transparent",
  "border:0",
  "border-bottom:1px solid var(--rule)",
  "padding:.4rem 0",
  "font-family:var(--font-mono)",
  "color:var(--ink)",
  "outline:none",
].join(";");

export async function renderProfile(host: HTMLElement): Promise<void> {
  host.innerHTML = `<div class="loading">Loading profile</div>`;

  let user: MeInfo;
  try {
    user = await getMe();
  } catch (e) {
    host.innerHTML = `<div class="empty">Could not load profile: ${escapeHtml((e as Error).message)}</div>`;
    return;
  }

  host.innerHTML = `
    <header class="page-head">
      <h1>${escapeHtml(user.username)}</h1>
      <div class="meta">— Your account</div>
    </header>

    <div class="section-head">
      <h2>Account info</h2>
      <span class="rule"></span>
    </div>
    <div class="panel">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1.5rem">
        <div>
          <div class="label">— Username</div>
          <div class="folio" style="font-size:clamp(1.5rem,3vw,2.5rem)">${escapeHtml(user.username)}</div>
        </div>
        <div>
          <div class="label">— Role</div>
          <div class="folio" style="font-size:clamp(1.5rem,3vw,2.5rem)">${user.is_admin ? "Admin" : "User"}</div>
        </div>
        <div>
          <div class="label">— Joined</div>
          <div style="font-family:var(--font-mono);font-size:var(--t-body);color:var(--muted);padding-top:.5rem">
            ${escapeHtml(user.created_at ? new Date(user.created_at * 1000).toLocaleDateString() : "—")}
          </div>
        </div>
        <div>
          <div class="label">— Password set</div>
          <div style="font-family:var(--font-mono);font-size:var(--t-body);color:var(--muted);padding-top:.5rem">
            ${escapeHtml(user.password_changed_at ? new Date(user.password_changed_at * 1000).toLocaleDateString() : "—")}
          </div>
        </div>
      </div>
    </div>

    <div class="section-head">
      <h2>Change password</h2>
      <span class="rule"></span>
    </div>
    <div class="panel">
      <form data-pwform style="display:grid;gap:1.25rem;max-width:360px">
        <div>
          <label class="label" style="display:block;margin-bottom:.4rem">— Current password</label>
          <input type="password" name="current" required autocomplete="current-password" style="${INPUT_STYLE}" />
        </div>
        <div>
          <label class="label" style="display:block;margin-bottom:.4rem">— New password</label>
          <input type="password" name="new" required autocomplete="new-password" style="${INPUT_STYLE}" />
        </div>
        <div>
          <label class="label" style="display:block;margin-bottom:.4rem">— Confirm new password</label>
          <input type="password" name="confirm" required autocomplete="new-password" style="${INPUT_STYLE}" />
        </div>
        <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
          <button type="submit" class="btn primary">Update password</button>
          <span data-pwmsg style="font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.15em;text-transform:uppercase;min-height:1em"></span>
        </div>
      </form>
    </div>
  `;

  const form = host.querySelector<HTMLFormElement>("[data-pwform]")!;
  const msg  = host.querySelector<HTMLElement>("[data-pwmsg]")!;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd      = new FormData(form);
    const current = String(fd.get("current") ?? "");
    const newPw   = String(fd.get("new") ?? "");
    const confirm = String(fd.get("confirm") ?? "");

    if (newPw !== confirm) {
      msg.style.color = "var(--accent)";
      msg.textContent = "Passwords don't match";
      return;
    }

    const submit = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
    submit.disabled = true;
    msg.style.color = "var(--muted)";
    msg.textContent = "Updating…";

    try {
      await changeOwnPassword(current, newPw);
      // Keep localStorage in sync so future Subsonic token-auth uses the new password.
      updateStoredPassword(newPw);
      msg.style.color = "var(--accent)";
      msg.textContent = "Password updated";
      form.reset();
    } catch (ex) {
      msg.style.color = "var(--accent)";
      msg.textContent = (ex as Error).message;
    } finally {
      submit.disabled = false;
    }
  });
}

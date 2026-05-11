// src/views/admin.ts
//
// Admin panel: list, create, edit and delete users.
// Only reachable by admins — guarded both here and by the nav.

import {
  listUsers, createUser, patchUser, deleteUser,
  type UserRecord,
} from "../api";
import { authState } from "../auth";
import { escapeHtml } from "./_util";

const INPUT_STYLE = [
  "background:transparent",
  "border:0",
  "border-bottom:1px solid var(--rule)",
  "padding:.4rem 0",
  "font-family:var(--font-mono)",
  "color:var(--ink)",
  "outline:none",
].join(";");

export async function renderAdmin(host: HTMLElement): Promise<void> {
  if (!authState().is_admin) {
    location.hash = "#/";
    return;
  }

  host.innerHTML = `
    <header class="page-head">
      <h1>Users</h1>
      <div class="meta">— User management</div>
    </header>

    <div class="section-head">
      <h2>All users</h2>
      <span class="rule"></span>
    </div>
    <div data-userlist class="loading">Loading users</div>
  `;

  await refreshUsers(host);
}

async function refreshUsers(host: HTMLElement): Promise<void> {
  const el = host.querySelector<HTMLElement>("[data-userlist]");
  if (!el) return;

  let users: UserRecord[];
  try {
    users = await listUsers();
  } catch (e) {
    el.classList.remove("loading");
    el.innerHTML = `<div class="empty">${escapeHtml((e as Error).message)}</div>`;
    return;
  }

  const me = authState().username;
  el.classList.remove("loading");

  el.innerHTML = `
    <div class="panel">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr>
            <th class="label" style="text-align:left;padding-bottom:.5rem;border-bottom:1px solid var(--rule)">— Username</th>
            <th class="label" style="text-align:left;padding-bottom:.5rem;border-bottom:1px solid var(--rule)">— Role</th>
            <th class="label" style="text-align:left;padding-bottom:.5rem;border-bottom:1px solid var(--rule)">— Joined</th>
            <th class="label" style="text-align:left;padding-bottom:.5rem;border-bottom:1px solid var(--rule)">— Password set</th>
            <th style="border-bottom:1px solid var(--rule)"></th>
          </tr>
        </thead>
        <tbody>
          ${users.map(u => userRowHtml(u, u.username === me)).join("")}
        </tbody>
      </table>
    </div>

    <div class="section-head" style="margin-top:2rem">
      <h2>Add user</h2>
      <span class="rule"></span>
    </div>
    <div class="panel">
      <form data-addform style="display:grid;grid-template-columns:1fr 1fr auto auto;gap:.75rem;align-items:end">
        <div>
          <label class="label" style="display:block;margin-bottom:.4rem">— Username</label>
          <input name="username" required placeholder="alice" style="${INPUT_STYLE};width:100%" />
        </div>
        <div>
          <label class="label" style="display:block;margin-bottom:.4rem">— Password</label>
          <input name="password" type="password" required style="${INPUT_STYLE};width:100%" />
        </div>
        <label style="display:flex;align-items:center;gap:.4rem;font-family:var(--font-mono);font-size:var(--t-small);padding-bottom:.4rem;cursor:pointer;white-space:nowrap">
          <input type="checkbox" name="is_admin" /> Admin
        </label>
        <button type="submit" class="btn primary">Create →</button>
      </form>
      <div data-addmsg style="margin-top:.75rem;font-family:var(--font-mono);font-size:var(--t-micro);letter-spacing:.1em;text-transform:uppercase;min-height:1em"></div>
    </div>
  `;

  el.querySelectorAll<HTMLButtonElement>("[data-toggle-admin]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.toggleAdmin);
      const makeAdmin = btn.dataset.makeAdmin === "1";
      btn.disabled = true;
      try {
        await patchUser(id, { is_admin: makeAdmin });
        await refreshUsers(host);
      } catch (e) {
        alert((e as Error).message);
        btn.disabled = false;
      }
    });
  });

  el.querySelectorAll<HTMLButtonElement>("[data-reset-pw]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.resetPw);
      const newPw = prompt("New password for this user:");
      if (!newPw) return;
      btn.disabled = true;
      try {
        await patchUser(id, { password: newPw });
        const orig = btn.textContent;
        btn.textContent = "✓ Done";
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1500);
      } catch (e) {
        alert((e as Error).message);
        btn.disabled = false;
      }
    });
  });

  el.querySelectorAll<HTMLButtonElement>("[data-del]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.del);
      const uname = btn.dataset.username ?? "this user";
      if (!confirm(`Delete ${uname}? This cannot be undone.`)) return;
      btn.disabled = true;
      try {
        await deleteUser(id);
        await refreshUsers(host);
      } catch (e) {
        alert((e as Error).message);
        btn.disabled = false;
      }
    });
  });

  const form   = el.querySelector<HTMLFormElement>("[data-addform]")!;
  const addMsg = el.querySelector<HTMLElement>("[data-addmsg]")!;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd       = new FormData(form);
    const username = String(fd.get("username") ?? "").trim();
    const password = String(fd.get("password") ?? "");
    const is_admin = fd.get("is_admin") === "on";
    if (!username || !password) return;

    const submit = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
    submit.disabled = true;
    addMsg.style.color = "var(--muted)";
    addMsg.textContent = "Creating…";

    try {
      await createUser(username, password, is_admin);
      addMsg.style.color = "var(--accent)";
      addMsg.textContent = `Created ${username}`;
      form.reset();
      await refreshUsers(host);
    } catch (ex) {
      addMsg.style.color = "var(--accent)";
      addMsg.textContent = (ex as Error).message;
    } finally {
      submit.disabled = false;
    }
  });
}

function userRowHtml(u: UserRecord, isSelf: boolean): string {
  const joined = u.created_at
    ? new Date(u.created_at * 1000).toLocaleDateString()
    : "—";
  const pwChanged = u.password_changed_at
    ? new Date(u.password_changed_at * 1000).toLocaleDateString()
    : "—";

  const adminToggle = isSelf ? "" : u.is_admin
    ? `<button class="btn ghost" data-toggle-admin="${u.id}" data-make-admin="0"
         style="padding:.35rem .65rem;font-size:var(--t-micro)">Revoke admin</button>`
    : `<button class="btn ghost" data-toggle-admin="${u.id}" data-make-admin="1"
         style="padding:.35rem .65rem;font-size:var(--t-micro)">Make admin</button>`;

  const resetPw = `<button class="btn ghost" data-reset-pw="${u.id}"
    style="padding:.35rem .65rem;font-size:var(--t-micro)">Reset pw</button>`;

  const delBtn = isSelf ? "" :
    `<button class="btn ghost" data-del="${u.id}" data-username="${escapeHtml(u.username)}"
       style="padding:.35rem .65rem;font-size:var(--t-micro);color:var(--accent)">Delete</button>`;

  return `
    <tr>
      <td style="padding:.75rem 1rem .75rem 0;font-family:var(--font-display);border-bottom:1px solid var(--rule)">
        ${escapeHtml(u.username)}
        ${isSelf ? `<span class="label" style="margin-left:.5rem">— you</span>` : ""}
      </td>
      <td style="padding:.75rem 1rem .75rem 0;font-family:var(--font-mono);font-size:var(--t-small);border-bottom:1px solid var(--rule)">
        ${u.is_admin ? "Admin" : "User"}
      </td>
      <td style="padding:.75rem 1rem .75rem 0;font-family:var(--font-mono);font-size:var(--t-small);color:var(--muted);border-bottom:1px solid var(--rule)">
        ${escapeHtml(joined)}
      </td>
      <td style="padding:.75rem 1rem .75rem 0;font-family:var(--font-mono);font-size:var(--t-small);color:var(--muted);border-bottom:1px solid var(--rule)">
        ${escapeHtml(pwChanged)}
      </td>
      <td style="padding:.75rem 0;border-bottom:1px solid var(--rule)">
        <div style="display:flex;gap:.4rem;justify-content:flex-end;flex-wrap:wrap">
          ${adminToggle}
          ${resetPw}
          ${delBtn}
        </div>
      </td>
    </tr>
  `;
}

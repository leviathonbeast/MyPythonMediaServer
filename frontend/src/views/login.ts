// src/views/login.ts
//
// The login screen.
//
// Editorial-style split layout: a "masthead" panel on the left that sets
// the tone of the app (this is the first thing users see), and a minimal
// form on the right. We keep the form deliberately quiet — the tone is
// set by the masthead.

import { login } from "../auth";

export function renderLogin(host: HTMLElement): void {
  host.innerHTML = `
    <section class="login stagger">
      <aside class="marquee">
        <div class="top">
          <span class="label">— Muse / Vol. 01</span>
        </div>
        <div>
          <h1 class="heading">A library<br/>for <em>sound.</em></h1>
          <p class="sub">
            A self-hosted music server, Subsonic-compatible, designed to
            scale from a hard drive to a hundred thousand records.
          </p>
        </div>
        <div class="colophon">
          MUSE / OPEN-SOURCE / FFMPEG-POWERED<br/>
          BUILT FOR PERSONAL ARCHIVES
        </div>
      </aside>

      <div class="form-side">
        <form data-form>
          <div>
            <span class="label">— Sign in / no. 001</span>
          </div>

          <div class="field">
            <label class="label" for="u">Username</label>
            <input id="u" name="username" autocomplete="username" required autofocus />
          </div>

          <div class="field">
            <label class="label" for="p">Password</label>
            <input id="p" name="password" type="password" autocomplete="current-password" required />
          </div>

          <div class="err" data-err></div>

          <button type="submit" class="btn primary">Enter the library →</button>
        </form>
      </div>
    </section>
  `;

  const form = host.querySelector<HTMLFormElement>("[data-form]")!;
  const err = host.querySelector<HTMLElement>("[data-err]")!;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    const data = new FormData(form);
    const username = String(data.get("username") ?? "").trim();
    const password = String(data.get("password") ?? "");
    if (!username || !password) return;

    const submit = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
    submit.disabled = true;
    submit.textContent = "Authenticating…";
    try {
      await login(username, password);
      // On success: jump to the library. The router (in main.ts) listens
      // to hashchange and will re-render under the authenticated shell.
      location.hash = "#/";
    } catch (ex) {
      err.textContent = (ex as Error).message;
      submit.disabled = false;
      submit.textContent = "Enter the library →";
    }
  });
}

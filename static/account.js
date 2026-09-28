// Shared account menu: fills #account and exposes the signed-in user as window.account.me.
// Pages listen for the "account" event (detail = user or null) to show or hide signed-in features.
(() => {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const box = document.getElementById("account");
  const state = window.account = { me: null };

  async function load() {
    try {
      state.me = (await (await fetch("/api/me")).json()).user;
    } catch {
      state.me = null;
    }
    render();
    document.dispatchEvent(new CustomEvent("account", { detail: state.me }));
    return state.me;
  }
  state.reload = load;

  function render() {
    const back = encodeURIComponent(location.pathname);
    if (!state.me) {
      box.innerHTML = `<a class="btn small ghost" href="/login?next=${back}">Sign in</a>`
        + `<a class="btn small" href="/signup?next=${back}">Create account</a>`;
      return;
    }
    const me = state.me;
    const initials = me.name.split(/\s+/).filter(Boolean).map(w => w[0]).join("").slice(0, 2).toUpperCase();
    box.innerHTML = `<details class="account">
      <summary aria-label="Account menu"><span class="avatar" aria-hidden="true">${esc(initials)}</span><span>${esc(me.name.split(" ")[0])}</span></summary>
      <div class="menu">
        <div class="who"><b>${esc(me.name)}</b>${esc(me.email)}${me.role === "admin" ? " · Administrator" : ""}</div>
        ${location.pathname !== "/" ? '<a href="/">Live map</a>' : ""}
        ${me.role === "admin" && location.pathname !== "/admin" ? '<a href="/admin">Admin</a>' : ""}
        <button type="button" data-act="password">Change password</button>
        <button type="button" data-act="logout">Sign out</button>
      </div></details>`;
  }

  box.addEventListener("click", async ev => {
    const act = ev.target.closest("[data-act]")?.dataset.act;
    if (act === "logout") {
      await post("/api/logout", {});
      location.href = "/";
    } else if (act === "password") {
      box.querySelector("details").open = false;
      passwordDialog();
    }
  });
  document.addEventListener("click", ev => {  // close the menu when clicking elsewhere
    const open = box.querySelector("details[open]");
    if (open && !open.contains(ev.target)) open.open = false;
  });

  function passwordDialog() {
    let dlg = document.getElementById("pw-dialog");
    if (!dlg) {
      dlg = Object.assign(document.createElement("dialog"), { id: "pw-dialog" });
      dlg.innerHTML = `<form class="dlg" id="pw-form">
        <h2>Change password</h2>
        <label class="field">Current password <input type="password" id="pw-current" autocomplete="current-password" required></label>
        <label class="field">New password <input type="password" id="pw-new" autocomplete="new-password" required minlength="8" maxlength="128">
          <span class="hint">At least 8 characters. Your other devices will be signed out.</span></label>
        <p class="msg" id="pw-msg" role="alert"></p>
        <div class="actions"><button type="button" class="btn ghost" id="pw-cancel">Cancel</button><button class="btn" id="pw-save">Change password</button></div>
      </form>`;
      document.body.append(dlg);
      dlg.querySelector("#pw-cancel").addEventListener("click", () => dlg.close());
      dlg.querySelector("#pw-form").addEventListener("submit", async ev => {
        ev.preventDefault();
        const msg = dlg.querySelector("#pw-msg"), save = dlg.querySelector("#pw-save");
        save.disabled = true;
        try {
          const res = await post("/api/me/password", { current: dlg.querySelector("#pw-current").value, new: dlg.querySelector("#pw-new").value });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error);
          msg.className = "msg ok";
          msg.textContent = "Password changed.";
          setTimeout(() => dlg.close(), 900);
        } catch (err) {
          msg.className = "msg err";
          msg.textContent = err.message;
        } finally {
          save.disabled = false;
        }
      });
    }
    dlg.querySelector("#pw-form").reset();
    dlg.querySelector("#pw-msg").textContent = "";
    dlg.showModal();
  }

  load();
})();

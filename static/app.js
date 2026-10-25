"use strict";

const root = document.getElementById("app");
const state = { me: null, view: "discover", kind: "manga", query: "", items: [], results: {}, have: {}, filters: {} };

const KIND = { manga: "Manga", novel: "Novela ligera" };
const STATUS = { pending: "Pendiente", approved: "Aprobado", grabbed: "Descargando", denied: "Rechazado" };

// Crea elementos sin innerHTML: todo el texto que viene de afuera va como texto.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

function toast(msg, bad) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = ""), 4000);
}

async function api(path, body) {
  const opts = { headers: { "Content-Type": "application/json" } };
  if (body !== undefined) Object.assign(opts, { method: "POST", body: JSON.stringify(body) });
  const res = await fetch("api/" + path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "login") { state.me = null; render(); }
  if (!res.ok) throw Object.assign(new Error(data.error || "Error " + res.status), { data });
  return data;
}

// Ejecuta una acción mostrando el error si falla.
async function run(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
}

const plain = (html) => new DOMParser().parseFromString(html || "", "text/html").body.textContent;
const size = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(0) + " MB");
const badge = (s) => h("span", { class: "badge " + s }, STATUS[s] || s);
// Lo que ya hay en Komga: completo, parcial (libros/volúmenes) o en biblioteca si AniList no sabe cuántos son.
const availBadge = (a) => a && h("span", { class: "badge " + (a.complete ? "grabbed" : "approved") },
  a.complete ? "Disponible" : a.volumes ? `Parcial ${a.books}/${a.volumes}` : `En biblioteca · ${a.books}`);
// Si ya está en Komga, eso reemplaza a "Descargando"/"Aprobado"; pendientes y rechazados se muestran igual.
const statusBadge = (status, available) =>
  available && !["pending", "denied"].includes(status)
    ? availBadge(available)
    : [availBadge(available), status && badge(status)];

// ------------------------------------------------------------------ vistas

function loginView() {
  const user = h("input", { placeholder: "Usuario de Komga (email)", autocomplete: "username", required: true });
  const pass = h("input", { type: "password", placeholder: "Contraseña", autocomplete: "current-password", required: true });
  return h("form", {
    class: "login",
    onsubmit: (e) => {
      e.preventDefault();
      run(async () => {
        await api("login", { username: user.value, password: pass.value });
        state.me = await api("me");
        render();
      });
    },
  },
    h("h1", {}, "Taberna · Pedidos"),
    h("p", { class: "muted" }, "Entrá con tu cuenta de Komga."),
    user, pass, h("button", { class: "primary" }, "Entrar"));
}

function shell(content) {
  const tab = (id, label) =>
    h("button", { class: "tab" + (state.view === id ? " on" : ""), onclick: () => { state.view = id; render(); } }, label);
  return h("div", {},
    h("header", {},
      h("strong", { class: "brand" }, "Taberna · Pedidos"),
      h("nav", {}, tab("discover", "Descubrir"), tab("requests", "Pedidos"), !!state.me.is_admin && tab("users", "Usuarios")),
      h("span", { class: "who" }, state.me.email,
        h("button", { class: "link", onclick: () => run(async () => { await api("logout", {}); state.me = null; render(); }) }, "Salir"))),
    h("main", {}, content));
}

function discoverView() {
  const q = h("input", { type: "search", placeholder: "Buscar en AniList…", value: state.query });
  const kindBtn = (k) =>
    h("button", { type: "button", class: "chip" + (state.kind === k ? " on" : ""), onclick: () => { state.kind = k; state.items = []; render(); } }, KIND[k]);
  const form = h("form", {
    class: "search",
    onsubmit: (e) => {
      e.preventDefault();
      state.query = q.value.trim();
      if (!state.query) return;
      run(async () => { state.items = await api(`anilist?kind=${state.kind}&q=${encodeURIComponent(state.query)}`); render(); });
    },
  }, kindBtn("manga"), kindBtn("novel"), q, h("button", { class: "primary" }, "Buscar"));
  const grid = h("div", { class: "grid" }, state.items.map(card));
  return h("section", {}, form, state.items.length ? grid : h("p", { class: "muted" }, "Buscá un manga o una novela ligera para pedirla."));
}

function card(it) {
  return h("button", { class: "card", onclick: () => openDetail(it) },
    it.cover ? h("img", { src: it.cover, alt: "", loading: "lazy" }) : h("div", { class: "nocover" }),
    h("span", { class: "title" }, it.title),
    h("span", { class: "muted small" }, [it.year, it.volumes && it.volumes + " vol."].filter(Boolean).join(" · ")),
    h("span", { class: "row" }, statusBadge(it.request_status, it.available)));
}

function openDetail(it) {
  const close = () => dlg.remove();
  const requestBtn = it.request_status
    ? badge(it.request_status)
    : it.available && it.available.complete
    ? h("span", { class: "muted" }, "Ya está completa en la biblioteca.")
    : h("button", {
      class: "primary",
      onclick: (e) => {
        const btn = e.currentTarget, label = btn.textContent;
        btn.disabled = true;
        btn.textContent = "Pidiendo…";  // respuesta inmediata, aunque el servidor tarde
        run(async () => {
          try {
            const r = await api("requests", it);
            it.request_status = r.status;
            toast(r.status === "approved" ? "✔ Pedido aprobado: buscando release…" : "✔ Pedido enviado: queda esperando aprobación.");
            close(); render();
          } catch (err) {
            btn.disabled = false;
            btn.textContent = label;
            throw err;
          }
        });
      },
    }, it.available ? "Pedir lo que falta" : "Pedir");
  const dlg = h("div", { class: "overlay", onclick: (e) => e.target === dlg && close() },
    h("div", { class: "modal" },
      h("button", { class: "x", onclick: close, "aria-label": "Cerrar" }, "×"),
      h("div", { class: "detail" },
        it.cover && h("img", { src: it.cover, alt: "" }),
        h("div", {},
          h("h2", {}, it.title),
          h("p", { class: "muted small" }, [KIND[it.kind], it.year, it.volumes && it.volumes + " volúmenes", it.status].filter(Boolean).join(" · ")),
          h("p", { class: "desc" }, plain(it.description)),
          h("div", { class: "row" }, availBadge(it.available), requestBtn, it.url && h("a", { href: it.url, target: "_blank", rel: "noopener" }, "Ver en AniList"))))));
  document.body.append(dlg);
}

function requestsView() {
  const list = h("div", { class: "list" }, h("p", { class: "muted" }, "Cargando…"));
  run(async () => {
    const rows = await api("requests");
    list.replaceChildren(...(rows.length ? rows.map(requestItem) : [h("p", { class: "muted" }, "Todavía no hay pedidos.")]));
  });
  return h("section", {}, list);
}

function requestItem(r) {
  const me = state.me;
  const admin = !!me.is_admin;
  const owner = r.requested_by === me.email;
  const chooser = admin || (owner && !!me.can_choose);
  const results = h("div", { class: "results" });
  const act = (label, action, cls) =>
    h("button", { class: cls || "", onclick: () => run(async () => { await api(`requests/${r.id}/${action}`, {}); render(); }) }, label);

  const actions = [
    admin && r.status === "pending" && act("Aprobar", "approve", "primary"),
    admin && r.status === "pending" && act("Rechazar", "deny"),
    chooser && r.status !== "denied" && h("button", { onclick: () => search(r, results) }, "Buscar release"),
    admin && r.status !== "denied" && act(r.releasing ? "Dejar de seguir" : "Seguir volúmenes nuevos", "follow"),
    (admin || (owner && r.status === "pending")) && act("Borrar", "delete", "danger"),
  ];
  return h("article", { class: "req" },
    r.cover ? h("img", { src: r.cover, alt: "" }) : h("div", { class: "nocover" }),
    h("div", { class: "grow" },
      h("div", { class: "row" }, h("strong", {}, r.title), statusBadge(r.status, r.available)),
      h("p", { class: "muted small" }, `${KIND[r.kind] || r.kind} · pedido por ${r.requested_by}`),
      r.release_title && h("p", { class: "small" }, `Release: ${r.release_title} (${r.release_indexer || "?"})`),
      !!r.releasing && h("p", { class: "small muted" }, "📡 En seguimiento: se buscan volúmenes nuevos solos cada día."),
      r.note && h("p", { class: "small note" }, r.note),
      h("div", { class: "row" }, actions),
      results));
}

function search(r, box) {
  box.replaceChildren(h("p", { class: "muted" }, "Buscando en los trackers…"));
  run(async () => {
    try {
      const data = await api(`requests/${r.id}/search`, {});
      state.results[r.id] = data.results;
      state.have[r.id] = data.have;
      renderResults(r, box);
    } catch (e) { box.replaceChildren(); throw e; }
  });
}

function renderResults(r, box) {
  const all = state.results[r.id] || [];
  const f = (state.filters[r.id] = state.filters[r.id] || { indexer: "", lang: "" });
  const langsOf = (x) => (x.languages && x.languages.length ? x.languages : ["?"]);
  const shown = all.filter((x) => (!f.indexer || x.indexer === f.indexer) && (!f.lang || langsOf(x).includes(f.lang)));
  const picker = (key, label, values) => h("select", { onchange: (e) => { f[key] = e.target.value; renderResults(r, box); } },
    h("option", { value: "" }, label),
    values.map((v) => h("option", { value: v, selected: v === f[key] }, v === "?" ? "Sin indicar" : v)));
  const select = [
    picker("indexer", `Todos los trackers (${all.length})`, [...new Set(all.map((x) => x.indexer))]),
    picker("lang", "Todos los idiomas", [...new Set(all.flatMap(langsOf))]),
  ];
  const rows = shown.map((x) => h("tr", { class: x.match && !x.avoid ? "" : "dim" },
    h("td", {}, x.infoUrl ? h("a", { href: x.infoUrl, target: "_blank", rel: "noopener" }, x.title) : x.title,
      !x.match && h("span", { class: "muted small" }, " (no coincide con el título)"),
      x.match && x.wrong_kind && h("span", { class: "muted small" }, r.kind === "novel" ? " (el tracker lo clasifica como manga)" : " (el tracker lo clasifica como novela)"),
      x.match && x.avoid && !x.wrong_kind && h("span", { class: "muted small" }, " (web novel, audiolibro o traducción automática)")),
    h("td", {}, x.indexer),
    h("td", { class: "num" }, x.vol_from == null ? "?" : x.vol_from === x.vol_to ? `${x.vol_from}` : `${x.vol_from}–${x.vol_to}`,
      x.only_new ? h("span", { class: "badge grabbed" }, "nuevos") : x.new ? h("span", { class: "badge approved" }, "incluye nuevos") : "",
      x.complete && h("span", { class: "badge grabbed" }, "completo")),
    h("td", {}, langsOf(x).join(", ")),
    h("td", { class: "num" }, size(x.size || 0)),
    h("td", { class: "num" }, `${x.seeders ?? "?"} / ${x.leechers ?? "?"}`),
    h("td", {}, h("button", {
      class: "primary",
      onclick: () => run(async () => {
        await api(`requests/${r.id}/grab`, { guid: x.guid });
        toast("Enviado a qBittorrent.");
        render();
      }),
    }, "Descargar"))));
  box.replaceChildren(
    h("div", { class: "row" }, select, h("span", { class: "muted small" }, "Los atenuados no coinciden con el título o conviene evitarlos.")),
    !!state.have[r.id] && h("p", { class: "small" },
      `Ya tenés ${state.have[r.id]} en Komga. "nuevos" = trae solo lo que falta; "incluye nuevos" = pack que repite lo que ya tenés.`),
    shown.length
      ? h("table", {}, h("thead", {}, h("tr", {}, ["Release", "Tracker", "Vol.", "Idioma", "Tamaño", "Seeds / Peers", ""].map((t) => h("th", {}, t)))), h("tbody", {}, rows))
      : h("p", { class: "muted" }, "Sin resultados."));
}

function usersView() {
  const list = h("div", { class: "list" }, h("p", { class: "muted" }, "Cargando…"));
  run(async () => {
    const users = await api("users");
    list.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Usuario", "Admin", "Auto-aprobar", "Elegir release"].map((t) => h("th", {}, t)))),
      h("tbody", {}, users.map((u) => h("tr", {},
        h("td", {}, u.email),
        ["is_admin", "auto_approve", "can_choose"].map((k) => h("td", {},
          h("input", {
            type: "checkbox", checked: !!u[k], "aria-label": k,
            onchange: (e) => run(async () => { await api(`users/${encodeURIComponent(u.email)}`, { [k]: e.target.checked }); toast("Guardado."); }),
          }))))))));
  });
  return h("section", {},
    h("p", { class: "muted small" }, "Auto-aprobar: el pedido se busca y descarga solo. Elegir release: puede ver los resultados de cada tracker y elegir."),
    list);
}

function render() {
  if (!state.me) return root.replaceChildren(loginView());
  const views = { discover: discoverView, requests: requestsView, users: usersView };
  root.replaceChildren(shell((views[state.view] || discoverView)()));
}

api("me").then((me) => { state.me = me; }).catch(() => {}).finally(render);

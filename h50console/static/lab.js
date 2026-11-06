// Laboratório: ferramentas para explorar o que os manuais não dizem.
// Propriedades · Mudanças (com anotações) · Varredura · Ações (por risco) · Objetos da nuvem.
// Leituras são livres; escrita e ações exigem RAW_ENABLED=true no servidor.
(function () {
  const H = window.H50;
  const { $, el, api, toast, run, openSheet, confirmAction, chip, fmtDate } = H;

  const TABS = [["props", "Propriedades"], ["changes", "Mudanças"], ["scan", "Varredura"], ["actions", "Ações"], ["cloud", "Nuvem"]];
  const RISK = { safe: ["consulta", ""], acts: ["faz algo", "warn"], destructive: ["destrutiva", "warn"] };
  const first = new URLSearchParams(location.search).get("lab");        // ?lab=changes abre direto (capturas de tela)
  let tab = TABS.some(([id]) => id === first) ? first : "props", timer = null;

  async function laboratorio() {
    clearTimeout(timer);
    const bar = $("#lab-tabs");
    bar.replaceChildren(...TABS.map(([id, label]) => {
      const b = el("button", { className: tab === id ? "on" : "", textContent: label });
      b.onclick = () => { tab = id; laboratorio(); };
      return b;
    }));
    $("#lab-raw-warn").hidden = H.rawEnabled;
    const body = $("#lab-body");
    body.replaceChildren();
    await ({ props, changes, scan, actions, cloud }[tab])(body);
  }

  const toolbar = (...kids) => el("div", { className: "bar" }, ...kids);
  const btn = (text, fn, cls = "") => { const b = el("button", { textContent: text, className: cls }); b.onclick = fn; return b; };

  // ---- Propriedades --------------------------------------------------------------------------------------
  async function props(host) {
    const out = el("div");
    const render = async () => {
      const list = await api("GET", "/api/lab/props");
      Table.render(out, [
        { key: "key", label: "siid/piid" },
        { key: "name", label: "Nome", render: (r) => {
          const s = el("span", {}, r.name);
          if (r.discovered) s.append(el("span", { className: "pill-tag ok", textContent: "descoberta" }));
          else if (!r.in_spec) s.append(el("span", { className: "pill-tag warn", textContent: "fora da spec" }));
          return s;
        } },
        { key: "service", label: "Serviço" },
        { key: "format", label: "Tipo" },
        { key: "access", label: "Acesso", render: (r) => r.access.map((a) => ({ read: "R", write: "W", notify: "N" }[a] || a)).join("") },
        { key: "value", label: "Valor", render: (r) => r.value ?? "—" },
        { key: "edit", label: "", filter: false, sort: false, render: (r) => r.writable
          ? btn("Editar", () => edit(r, render)) : el("span") },
      ], list, { sort: "key" });
    };
    host.append(toolbar(
      btn("Ler tudo agora", () => run("Leitura", async () => { await api("POST", "/api/lab/read", {}); await render(); }), "primary"),
      el("span", { className: "muted small", textContent: "R = leitura, W = escrita, N = notifica. O valor vem do cache; \"Ler tudo\" consulta o robô." })), out);
    await render();
  }

  function edit(r, after) {
    if (!H.rawEnabled) return toast("Escrita desligada: inicie o app com RAW_ENABLED=true", true);
    let input;
    if (r.format === "bool") input = el("input", { type: "checkbox", checked: r.value === "true" });
    else if (r.options.length) {
      input = el("select", {}, ...r.options.map(([v, l]) => el("option", { value: v, textContent: `${l} (${v})`, selected: String(v) === r.value })));
    } else input = el("input", { value: r.value ?? "", type: r.format === "string" ? "text" : "number" });
    openSheet(`Escrever ${r.key} · ${r.name}`, [
      el("p", { className: "warn small", textContent: "Escreve direto no robô. Valores fora do esperado podem mudar o comportamento dele." }), input], {
      confirmLabel: "Escrever",
      confirm: async () => {
        const value = r.format === "bool" ? input.checked : r.format === "string" ? input.value : Number(input.value);
        const res = await api("POST", "/api/raw/set", { key: r.key, value });
        toast(`${r.key} = ${JSON.stringify(res.value ?? value)} (código ${res.code})`);
        await after();
      },
    });
  }

  // ---- Mudanças + anotações -----------------------------------------------------------------------------------
  async function changes(host) {
    let minutes = 60, noisy = false;
    const out = el("div");
    const note = el("input", { placeholder: 'Anotação, ex.: "mudei a secagem para 3 h no app"', size: 44, maxLength: 200 });
    const render = async () => {
      const d = await api("GET", `/api/lab/changes?minutes=${minutes}&noisy=${noisy ? 1 : 0}`);
      const rows = [
        ...d.notes.map((n) => ({ ts: n.ts, kind: "note", prop: "Anotação", name: "", old: "", new: n.text })),
        ...d.changes.map((c) => ({ ts: c.ts, kind: "change", prop: c.prop, name: c.name, old: c.old, new: c.new })),
      ].sort((a, b) => b.ts.localeCompare(a.ts));
      Table.render(out, [
        { key: "ts", label: "Quando", render: (r) => minutes > 1440 ? new Date(r.ts).toLocaleString("pt-BR")
          : new Date(r.ts).toLocaleTimeString("pt-BR"), sortValue: (r) => r.ts },
        { key: "prop", label: "Propriedade", render: (r) => r.kind === "note" ? el("strong", { textContent: "Anotação" })
          : el("span", {}, r.prop, el("span", { className: "muted small", textContent: r.name ? ` ${r.name}` : "" })) },
        { key: "old", label: "Antes" },
        { key: "new", label: "Depois" },
      ], rows, { rowClass: (r) => r.kind === "note" ? "flash" : "", sort: "ts", desc: true, filters: false });
      if (d.truncated) out.prepend(el("p", { className: "muted small", textContent: `Mostrando só as ${d.changes.length} mudanças mais recentes da janela.` }));
      clearTimeout(timer);
      // janelas de dias não se atualizam sozinhas (a consulta é grande e o que interessa já passou)
      if (minutes <= 1440) timer = setTimeout(() => location.hash === "#laboratorio" && tab === "changes" && render().catch(() => {}), 5000);
    };
    const range = el("select", {}, ...[[15, "15 min"], [60, "1 h"], [240, "4 h"], [1440, "24 h"], [4320, "3 dias"], [10080, "7 dias"]]
      .map(([v, l]) => el("option", { value: v, textContent: l, selected: v === minutes })));
    range.onchange = () => { minutes = Number(range.value); render(); };
    const noise = el("input", { type: "checkbox" });
    noise.onchange = () => { noisy = noise.checked; render(); };
    const add = () => note.value.trim() && run("Anotação", async () => { await api("POST", "/api/lab/notes", { text: note.value }); note.value = ""; await render(); });
    note.onkeydown = (e) => { if (e.key === "Enter") add(); };
    host.append(
      el("p", { className: "muted small", textContent: "Faça uma mudança no app oficial, anote aqui e veja quais propriedades mexeram logo depois: é assim que se descobre o que cada opção do app grava no robô." }),
      toolbar(note, btn("Marcar agora", add, "primary")),
      toolbar(el("label", {}, "Janela ", range), el("label", {}, noise, " mostrar o que muda sozinho (progresso, bateria…)"),
        el("span", { className: "muted small", textContent: "atualiza a cada 5 s (janelas até 24 h)" })), out);
    await render();
  }

  // ---- Varredura ----------------------------------------------------------------------------------------------------
  async function scan(host) {
    const f = { siid: el("input", { type: "number", min: 1, max: 99, value: 21, style: "width:5em" }),
      first: el("input", { type: "number", min: 1, value: 1, style: "width:5em" }),
      last: el("input", { type: "number", min: 1, value: 20, style: "width:5em" }) };
    const out = el("div");
    const go = async () => {
      const r = await api("POST", "/api/lab/scan", { siid: Number(f.siid.value), first: Number(f.first.value), last: Number(f.last.value) });
      const found = r.results.filter((x) => x.value !== null);
      host.querySelector(".scan-sum").textContent = `${found.length} de ${r.results.length} responderam (${r.via === "cloud" ? "pela nuvem: só enxerga o que está na spec" : "pela rede local"}); ${r.results.filter((x) => x.new).length} nova(s).`;
      Table.render(out, [
        { key: "key", label: "siid/piid" },
        { key: "code", label: "Código", num: true },
        { key: "value", label: "Valor", render: (x) => x.value ?? "—" },
        { key: "known", label: "", render: (x) => x.new ? chip("nova", "on") : x.known ? "conhecida" : "" },
      ], r.results, { filters: false, rowClass: (x) => x.new ? "flash" : "" });
    };
    host.append(
      el("p", { className: "muted small", textContent: "Lê uma faixa de piids de um serviço (somente leitura, até 40 por vez). O que responder e não estiver no catálogo vira \"descoberta\" e aparece em Propriedades. Só funciona pela rede local." }),
      toolbar(el("label", {}, "serviço (siid) ", f.siid), el("label", {}, "de ", f.first), el("label", {}, "até ", f.last),
        btn("Varrer", () => run("Varredura", go), "primary")),
      el("p", { className: "muted small scan-sum" }), out);
  }

  // ---- Ações ----------------------------------------------------------------------------------------------------------
  async function actions(host) {
    const list = await api("GET", "/api/lab/actions");
    const out = el("div");
    Table.render(out, [
      { key: "key", label: "siid/aiid" },
      { key: "name", label: "Ação" },
      { key: "service", label: "Serviço" },
      { key: "params", label: "Parâmetros", render: (a) => a.params.map((p) => `${p.name} (${p.piid})`).join(", ") || "—" },
      { key: "risk", label: "Risco", render: (a) => chip(RISK[a.risk][0], RISK[a.risk][1]) },
      { key: "run", label: "", filter: false, sort: false, render: (a) => btn("Executar", () => runAction(a)) },
    ], list, { sort: "key" });
    host.append(el("p", { className: "muted small", textContent: "Consulta = só lê · faz algo = move o robô ou a base (ou não se sabe o que faz) · destrutiva = altera ou apaga configuração salva (mapa, cômodos, agenda)." }), out);
  }

  function runAction(a) {
    if (!H.rawEnabled) return toast("Ações desligadas: inicie o app com RAW_ENABLED=true", true);
    const params = el("input", { placeholder: a.params.length ? `JSON, ex.: ${JSON.stringify(a.params.map(() => "valor"))}` : "(sem parâmetros)", size: 40, disabled: !a.params.length });
    const typed = el("input", { placeholder: `Digite ${a.name} para confirmar` });
    const result = el("pre", { className: "out", textContent: "" });
    const body = [el("p", {}, chip(RISK[a.risk][0], RISK[a.risk][1]), ` ${a.key} · ${a.name}`)];
    if (a.risk === "acts") body.push(el("p", { className: "warn small", textContent: "Esta ação pode mover o robô ou a base. Fique por perto." }));
    if (a.risk === "destructive") body.push(el("p", { className: "err small", textContent: "Esta ação pode APAGAR ou alterar configuração salva no robô (mapa, cômodos, agenda). Tenha um backup do mapa antes." }), typed);
    body.push(params, result);
    openSheet(`Executar ${a.key}`, body, {
      confirmLabel: "Executar",
      confirm: async () => {
        if (a.risk === "destructive" && typed.value.trim() !== a.name) throw new Error(`digite ${a.name} para confirmar`);
        let p = [];
        if (a.params.length) { try { p = JSON.parse(params.value || "[]"); } catch { throw new Error("parâmetros: JSON inválido"); } }
        const r = await api("POST", "/api/raw/action", { key: a.key, params: p });
        result.textContent = JSON.stringify(r, null, 2);
        toast(`${a.key}: código ${r.code ?? "?"}`);
        throw new Error("ação enviada (veja o resultado abaixo)");     // mantém o menu aberto para mostrar o retorno
      },
    });
  }

  // ---- Objetos da nuvem --------------------------------------------------------------------------------------------------
  async function cloud(host) {
    const out = el("div", { className: "muted", textContent: "Consultando a nuvem…" });
    host.append(el("p", { className: "muted small", textContent: "Objetos que o robô guarda na nuvem Xiaomi: mapa atual, trajetória (só existe durante a limpeza), mapa de tapetes e backups de mapa." }), out);
    let items;
    try { items = await api("GET", "/api/lab/cloud-objects"); } catch (e) { out.className = "err"; out.textContent = e.message; return; }
    out.className = "";
    Table.render(out, [
      { key: "label", label: "Objeto" },
      { key: "obj_name", label: "id" },
      { key: "bytes", label: "Tamanho", num: true, render: (i) => i.bytes ? `${i.bytes} B` : "—" },
      { key: "detail", label: "Conteúdo", render: (i) => i.empty ? "vazio" : i.error ? `${i.error}${i.hint ? ` (${i.hint})` : ""}`
        : i.decrypted ? `${i.rooms} cômodos · ${i.walls} paredes virtuais · ${i.furniture} móveis · ${i.path_points} pontos de caminho` : i.note },
      { key: "view", label: "", filter: false, sort: false, render: (i) => i.decrypted && i.obj ? btn("Ver", () => viewMap(i)) : el("span") },
    ], items, { filters: false });
  }

  async function viewMap(i) {
    const box = el("div", { textContent: "Carregando…", "data-maxh": "0.5" });
    openSheet(i.label, [box]);
    try {
      const m = await api("GET", `/api/lab/cloud-map?obj=${encodeURIComponent(i.obj)}`);
      box.textContent = "";
      await MapView.render(box, m, {});
    } catch (e) { box.textContent = e.message; }
  }

  H.views.laboratorio = laboratorio;
})();

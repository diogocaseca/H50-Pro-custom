// H50 Console: UI em JS puro, organizada como o app Xiaomi Home (prints de 11/10/2026).
// Início = tela principal do app; Estação/Modos/Atalhos/Área/Agenda abrem como menus que sobem de baixo.
(function () {
  const $ = (s) => document.querySelector(s);
  const el = (tag, props = {}, ...kids) => {
    const e = Object.assign(document.createElement(tag), props);
    for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(k ?? ""));
    return e;
  };

  // ---- API ----------------------------------------------------------------------------
  // Rotas da CONTA (robôs, nuvem, app) não levam o prefixo do robô; as demais vão para /api/r/<robô>/...
  let robotId = null;
  const ACCOUNT = /^\/api\/(robots(\/|$)|cloud\/(status|qr|logout|devices)$|app$|auth\/|maintenance\/|backup\/|notify(\/|$))/;   // /api/export/... é do robô ativo
  const url = (path) => {
    const p = path.split("?")[0];
    return ACCOUNT.test(p) || !robotId ? path : path.replace(/^\/api\//, `/api/r/${robotId}/`);
  };
  async function showPath(r) {
    const box = el("div", { textContent: "Carregando…" });
    openSheet(`Caminho de ${fmtDate(r.start)}`, [box]);
    try {
      const d = await api("GET", `/api/history/path?stime=${r.stime}`);
      if (!d.map) throw new Error("mapa ainda não baixado: abra o Início uma vez");
      box.textContent = "";
      await MapView.render(box, { ...d.map, _grid: undefined, path: d.points, robot: null }, {});
      box.append(el("p", { className: "muted small", textContent:
        `${d.points.length} pontos${d.final ? "" : " (parcial: a limpeza pode não ter terminado de gravar)"}.` }));
    } catch (e) { box.textContent = e.message; }
  }

  async function api(method, path, body) {
    const r = await fetch(url(path), {
      method, headers: { "X-H50": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401 && data.login) {          // sessão expirada ou ausente: volta para o login
      location.href = "/login.html?next=" + encodeURIComponent(location.pathname + location.hash);
      throw new Error("login necessário");
    }
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }
  // baixa um arquivo gerado pelo servidor (backup, CSV, diagnóstico)
  async function download(method, path, body) {
    const r = await fetch(url(path), {
      method, headers: { "X-H50": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.error || `HTTP ${r.status}`);
    }
    const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || "arquivo";
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(await r.blob()), download: name });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function toast(msg, err) {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), err ? 6000 : 2500);
  }
  async function run(label, fn) {
    try { await fn(); toast(`${label}: ok`); return true; } catch (e) { toast(`${label}: ${e.message}`, true); return false; }
  }
  const setProp = (key, value) => api("PUT", `/api/settings/${key.replace("/", "-")}`, { value });

  const fmtDate = (iso) => iso ? new Date(iso).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "medium" }) : "";
  const fmtDur = (s) => s == null ? "" : `${Math.floor(s / 60)} min`;
  const fmtMin = (m) => m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
  const ago = (iso) => {
    if (!iso) return "";
    const s = Math.round((Date.now() - new Date(iso)) / 1000);
    return s < 60 ? `há ${s} s` : s < 3600 ? `há ${Math.round(s / 60)} min` : fmtDate(iso);
  };
  const parse = (v, d = {}) => { try { return typeof v === "string" ? JSON.parse(v) : v ?? d; } catch { return d; } };

  // ---- estado compartilhado --------------------------------------------------------------
  let state = null, appInfo = null, settings = null, mapData = null, cloud = null, robots = [];
  const areaSel = new Set();                       // cômodos escolhidos em "Área" (vazio = casa inteira)
  const V = (k) => state?.values?.[k];
  const CLEANING = new Set([4, 10, 16, 17]), PAUSED = new Set([5, 18]), RETURNING = new Set([6, 19, 20, 21, 24]);
  const BASE_MODE = { 0: "", 1: "Secando", 2: "Esvaziando o pó", 3: "Lavando o mop" };

  async function loadState() { state = await api("GET", "/api/state"); return state; }
  async function loadSettings() { settings = await api("GET", "/api/settings"); return settings; }
  const setting = (k) => settings?.find((s) => s.key === k);
  const optLabel = (k, v) => (setting(k)?.options || []).find(([x]) => x === v)?.[1] ?? (v ?? "—");

  // ---- cabeçalho -------------------------------------------------------------------------
  function renderHeader() {
    if (!state) return;
    const h = state.health;
    $("#dot").className = "dot " + (h.online ? "on" : h.online === false ? "off" : "");
    $("#conn-sub").textContent = h.online ? "Conectado" : h.online === false ? "Sem resposta do robô" : "conectando…";
    $("#conn").textContent = h.online ? `${h.latency_ms ?? "–"} ms`
      : h.online === false ? `${h.fails} falhas · último contato ${ago(h.last_ok) || "nunca"}` : "";
  }

  // ---- Início -----------------------------------------------------------------------------
  function chip(text, cls = "") { return el("span", { className: "chip " + cls, textContent: text }); }

  function renderChips() {
    const st = state.status, base = parse(V("2/18"), { mode: 0 });
    if (state.status == null) {
      $("#chips").replaceChildren(chip(state.health.online === false ? "Sem resposta do robô" : "Conectando…", "warn"));
      return;
    }
    const out = [chip(state.status_label || "—", state.active ? "on" : "")];
    if (CLEANING.has(st) || PAUSED.has(st)) {
      const p = V("2/89"), area = (V("2/6") || 0) / 100;
      out.push(chip(`${p >= 0 ? p + " % · " : ""}${area.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} m² · ${fmtDur(V("2/7"))}`));
    }
    if (base.mode) {
      const extra = base.mode === 1 && V("2/90") ? ` · faltam ${fmtMin(V("2/90"))}` : base.progress ? ` · ${base.progress} %` : "";
      out.push(chip(BASE_MODE[base.mode] + extra));
    }
    if (V("3/1") != null) out.push(chip(`${V("3/1")} %` + (V("3/2") === 1 ? " · Carregando" : ""), V("3/1") < 20 ? "warn" : ""));
    if (V("2/97")) out.push(chip("Tanque de água suja cheio", "warn"));
    if (V("2/98")) out.push(chip("Tanque de água limpa vazio", "warn"));
    $("#chips").replaceChildren(...out);
  }

  function renderStartButtons() {
    const st = state.status;
    const label = CLEANING.has(st) ? "Pausar" : PAUSED.has(st) ? "Continuar" : RETURNING.has(st) ? "Parar" : "Iniciar limpeza";
    $("#start-label").textContent = label;
    $("#start-sub").textContent = label === "Iniciar limpeza" ? $("#sub-area").textContent : "";
    $("#active-row").hidden = !(CLEANING.has(st) || PAUSED.has(st));
    const offline = state.status == null;
    const blocked = label === "Iniciar limpeza" && (offline ? "robô sem resposta" : state.block_start);
    $("#btn-start").disabled = !!blocked;
    $("#blocked").hidden = !blocked;
    $("#blocked").textContent = blocked ? `Não dá para iniciar agora: ${state.block_start}.` : "";
  }

  function areaText() {
    if (!areaSel.size) return "Casa inteira";
    const names = Object.fromEntries((mapData?.rooms || roomsCache || []).map((r) => [r.id, r.name]));
    return [...areaSel].map((id) => names[id] || id).join(", ");
  }

  function renderSubs() {
    $("#sub-area").textContent = areaText();
    $("#sub-modes").textContent = V("2/4") == null ? "—"
      : [optLabel("2/4", V("2/4")), V("2/4") !== 1 ? optLabel("2/10", V("2/10")) : optLabel("2/9", V("2/9"))].join(" · ");
    $("#sub-shortcuts").textContent = V("2/83") == null ? "—" : optLabel("2/83", V("2/83"));
    const base = parse(V("2/18"), { mode: 0 });
    $("#sub-station").textContent = BASE_MODE[base.mode] || "Pronta";
  }

  // teste da limpeza por zona (ação 2/37): formato ainda não confirmado no H50 Pro
  const ZONE_DOCKED = new Set([2, 9, 14]);
  let zoneTesting = false;
  async function renderZoneTest() {
    const list = await api("GET", "/api/zones/captured").catch(() => []);
    const docked = ZONE_DOCKED.has(state?.status);
    $("#zone-list").replaceChildren(...(list.length ? list.map((z) => {
      const btn = el("button", { className: "play", textContent: "▶", disabled: zoneTesting || !docked,
        title: docked ? "Testar: limpar esta zona" : "Só com o robô na base" });
      btn.onclick = () => confirmAction("O robô vai sair da base para limpar a zona. Fique por perto para parar se precisar.",
        () => zoneTest(z.zones));
      const sizes = z.sizes.map(([w, h]) => `${w} × ${h} m`).join(", ");
      return el("div", { className: "list-row" },
        el("div", {}, el("div", { textContent: z.routine ? `Rotina "${z.routine}"` : `${z.zones.length} zona(s)` }),
          el("div", { className: "muted small", textContent: `${fmtDate(z.ts)} · ${sizes}` })),
        btn);
    }) : [el("p", { className: "muted small", textContent: "Nenhuma zona ainda. Rode uma limpeza por zona (ou a rotina) pelo app Xiaomi Home." })]));
  }

  async function zoneTest(zones) {
    const out = $("#zone-out"), goHome = $("#zone-home").checked;
    const log = (line) => { out.hidden = false; out.textContent += line + "\n"; };
    out.textContent = "";
    zoneTesting = true; renderZoneTest();
    try {
      const res = await api("POST", "/api/clean/zones", { zones });
      log(`resposta do robô: ${JSON.stringify(res)}`);
      if (res.code !== 0) { log("O robô recusou o formato. Nada foi iniciado."); return; }
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const st = await api("GET", "/api/state");
        const v = st.values || {};
        log(`${(i + 1) * 3} s: ${st.status_label} (${st.status}) · tipo 2/5 = ${v["2/5"]} · 2/40 = ${String(v["2/40"] ?? "").slice(0, 160)}`);
        if (v["2/5"] === 2 && String(v["2/40"]).includes("zones")) {
          log("FUNCIONOU: o robô aceitou e está limpando a zona (formato aceito).");
          if (goHome) { await api("POST", "/api/actions/home"); log("Mandei voltar à base."); }
          return;
        }
      }
      log("Em 60 s o robô não confirmou uma limpeza por zona. Confira onde ele está e, se precisar, mande voltar.");
    } catch (e) {
      log(`erro: ${e.message}`);
    } finally {
      zoneTesting = false; renderZoneTest();
    }
  }

  let roomsCache = null;
  async function renderHomeLists() {
    const [custom, sched] = await Promise.all([api("GET", "/api/custom"), api("GET", "/api/schedules")]);
    $("#custom-list").replaceChildren(...(custom.length ? custom.map((c) => {
      const play = el("button", { className: "play", title: `Executar ${c.name}`, textContent: "▶" });
      play.onclick = () => confirmAction(`Executar "${c.name}"? O robô vai sair da base.`,
        () => run(c.name, () => api("POST", `/api/clean/custom/${c.id}`)));
      return el("div", { className: "list-row" },
        el("div", {}, el("div", { textContent: c.name }), el("div", { className: "muted small", textContent: c.rooms.join(", ") || "zonas definidas no app" })),
        play);
    }) : [el("p", { className: "muted small", textContent: "Nenhuma rotina. Crie no app Xiaomi Home." })]));
    renderZoneTest();
    const on = sched.filter((s) => s.on).length;
    $("#sub-sched").textContent = `${sched.length} limpeza(s) programada(s)${sched.length ? `, ${on} ativa(s)` : ""}`;
    $("#btn-sched").onclick = () => openSheet("Limpeza programada", [
      ...sched.map((s) => el("div", { className: "list-row" },
        el("div", {}, el("div", { className: "big", textContent: s.time }),
          el("div", { className: "muted small", textContent: `${s.days.join(", ")}${s.repeat ? "" : " (uma vez)"} · ${s.routine}` })),
        chip(s.on ? "ativa" : "desligada", s.on ? "on" : ""))),
      el("p", { className: "muted small", textContent: "Somente leitura: crie e edite agendamentos no app Xiaomi Home." }),
    ]);
  }

  async function renderHomeMap() {
    cloud = await api("GET", "/api/cloud/status").catch(() => ({ state: "none" }));
    const logged = cloud.state === "ok";
    $("#cloud-connect").hidden = logged;
    $("#cloud-state").textContent = cloud.state === "waiting" ? "aguardando confirmação no celular…" : cloud.state === "error" ? `erro: ${cloud.error}` : "";
    $("#qr-box").hidden = !(cloud.state === "waiting" && cloud.qr);
    if (cloud.qr) { $("#qr-img").src = cloud.qr; $("#qr-link").href = cloud.qr; }
    $("#qr-start").onclick = () => run("QR", async () => { await api("POST", "/api/cloud/qr"); renderHomeMap(); });
    if (!logged) {   // sem nuvem: cômodos como blocos, clicáveis como no mapa
      roomsCache = roomsCache || await api("GET", "/api/rooms");
      $("#home-map").replaceChildren(el("div", { className: "room-grid" }, ...roomsCache.map((r) => {
        const b = el("button", { className: "room" + (areaSel.has(r.id) ? " on" : ""), textContent: r.name });
        b.onclick = () => toggleRoom(r.id);
        return b;
      })));
      return;
    }
    try {
      if (!mapData) mapData = await api("GET", "/api/map");
    } catch {
      await api("POST", "/api/cloud/map").catch(() => {});
      mapData = await api("GET", "/api/map").catch(() => null);
    }
    if (!mapData) { $("#home-map").textContent = "Não foi possível baixar o mapa."; return; }
    await drawMap();
  }

  async function drawMap() {
    const hover = $("#map-hover");
    await MapView.render($("#home-map"), mapData, {
      selected: areaSel,
      onRoomClick: toggleRoom,
      onHover: (e, name) => { hover.textContent = e && name ? `${name} · clique para escolher` : " "; },
    });
  }

  function toggleRoom(id) {
    areaSel.has(id) ? areaSel.delete(id) : areaSel.add(id);
    renderSubs(); renderStartButtons();
    if (mapData) drawMap(); else renderHomeMap();
  }

  function confirmAction(text, fn) {
    openSheet("Confirmar", [el("p", { textContent: text })], { confirm: async () => { await fn(); } });
  }

  async function startPressed() {
    const st = state.status;
    if (CLEANING.has(st)) return run("Pausar", () => api("POST", "/api/actions/pause"));
    if (PAUSED.has(st)) return run("Continuar", () => api("POST", "/api/actions/resume"));
    if (RETURNING.has(st)) return run("Parar", () => api("POST", "/api/actions/stop"));
    const where = areaText();
    confirmAction(`Iniciar limpeza: ${where}? O robô vai sair da base.`, () => areaSel.size
      ? run("Limpar cômodos", () => api("POST", "/api/clean/rooms", { ids: [...areaSel] }))
      : run("Iniciar limpeza", () => api("POST", "/api/actions/start")));
  }

  // ---- menus que sobem de baixo -------------------------------------------------------------
  function openSheet(title, body, { confirm, confirmLabel = "Confirmar" } = {}) {
    const d = $("#sheet");
    $("#sheet-title").textContent = title;
    $("#sheet-body").replaceChildren(...body);
    const foot = $("#sheet-foot");
    const cancel = el("button", { textContent: confirm ? "Cancelar" : "Fechar" });
    cancel.onclick = () => d.close();
    foot.replaceChildren(cancel);
    if (confirm) {
      const ok = el("button", { className: "primary", textContent: confirmLabel });
      ok.onclick = async () => {
        ok.disabled = true;
        try { await confirm(); d.close(); } catch (e) { toast(e.message, true); }   // erro: o menu fica aberto
        finally { ok.disabled = false; }
      };
      foot.append(ok);
    }
    if (!d.open) d.showModal();
    $("#sheet-title").focus();
  }
  $("#sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") e.target.close(); });   // toque fora fecha

  // grupo de opções em botões (como os círculos do app)
  function choiceGroup(title, options, current, onPick, { disabled = false } = {}) {
    const row = el("div", { className: "choices" + (disabled ? " disabled" : "") });
    for (const [v, label] of options) {
      const b = el("button", { className: "choice" + (v === current ? " on" : ""), textContent: label, disabled });
      b.onclick = () => { for (const x of row.children) x.classList.remove("on"); b.classList.add("on"); onPick(v); };
      row.append(b);
    }
    return el("div", { className: "sheet-section" }, el("div", { className: "sheet-h", textContent: title }), row);
  }

  async function openModes() {
    await loadSettings();
    const draft = { "2/4": V("2/4"), "2/9": V("2/9"), "2/10": V("2/10"), "2/74": V("2/74"), "2/8": V("2/8") };
    const body = () => {
      const mode = draft["2/4"];
      return [
        choiceGroup("Modo", setting("2/4").options, mode, (v) => { draft["2/4"] = v; openModesBody(); }),
        choiceGroup("Sucção", setting("2/9").options, draft["2/9"], (v) => (draft["2/9"] = v), { disabled: mode === 2 }),
        choiceGroup("Saída de água para esfregar", setting("2/10").options.filter(([v]) => v > 0), draft["2/10"],
          (v) => (draft["2/10"] = v), { disabled: mode === 1 }),
        choiceGroup("Caminho", setting("2/74").options, draft["2/74"], (v) => (draft["2/74"] = v)),
        choiceGroup("Tempos de limpeza", setting("2/8").options.filter(([v]) => v <= 2), draft["2/8"], (v) => (draft["2/8"] = v)),
      ];
    };
    const apply = async () => {
      const changed = Object.entries(draft).filter(([k, v]) => v !== V(k));
      for (const [k, v] of changed) await setProp(k, v);
      if (changed.length) toast(`Modos: ${changed.length} ajuste(s) gravado(s)`);
      await refreshHome();
    };
    const openModesBody = () => openSheet("Selecionar modo", body(), { confirm: apply });
    openModesBody();
  }

  async function openShortcuts() {
    await loadSettings();
    const s = setting("2/83");
    const rows = s.options.filter(([v]) => v <= 2).map(([v, label]) => {
      const b = el("button", { className: "list-choice" + (v === s.value ? " on" : "") },
        el("span", { textContent: label }),
        v === 0 ? el("span", { className: "warn small", textContent: "O pó não é esvaziado automaticamente." }) : "",
        el("span", { className: "check", textContent: v === s.value ? "✓" : "" }));
      b.onclick = async () => { if (await run("Atalho", () => setProp("2/83", v))) { $("#sheet").close(); refreshHome(); } };
      return b;
    });
    openSheet("Atalhos", rows);
  }

  function openArea() {
    const rooms = mapData?.rooms?.slice().sort((a, b) => a.id - b.id) || roomsCache || [];
    const draft = new Set(areaSel);
    const all = el("button", { className: "choice" + (draft.size ? "" : " on"), textContent: "Casa inteira" });
    const btns = rooms.map((r) => {
      // ajustes por cômodo (vêm no mapa da nuvem; 0 = usa o ajuste geral). Só leitura por enquanto.
      const own = [r.fan && `sucção ${r.fan}`, r.water && `água ${r.water}`, r.times && `${r.times}×`].filter(Boolean).join(", ");
      const b = el("button", { className: "choice" + (draft.has(r.id) ? " on" : ""), textContent: own ? `${r.name} · ${own}` : r.name,
        title: own ? "Este cômodo tem ajustes próprios (definidos no app Xiaomi Home)" : "Usa o ajuste geral" });
      b.onclick = () => { draft.has(r.id) ? draft.delete(r.id) : draft.add(r.id); b.classList.toggle("on"); all.classList.toggle("on", !draft.size); };
      return b;
    });
    all.onclick = () => { draft.clear(); btns.forEach((b) => b.classList.remove("on")); all.classList.add("on"); };
    openSheet("Área", [
      el("div", { className: "choices" }, all, ...btns),
      el("p", { className: "muted small", textContent: "Também dá para tocar nos cômodos do mapa." }),
    ], { confirm: () => { areaSel.clear(); draft.forEach((id) => areaSel.add(id)); toggleRefresh(); } });
  }
  const toggleRefresh = () => { renderSubs(); renderStartButtons(); mapData ? drawMap() : renderHomeMap(); };

  async function openStation(tab = "funcao") {
    await loadSettings();
    const base = parse(V("2/18"), { mode: 0 });
    const tabs = el("div", { className: "seg" });
    const mk = (id, label) => {
      const b = el("button", { className: tab === id ? "on" : "", textContent: label });
      b.onclick = () => openStation(id);
      return b;
    };
    tabs.append(mk("funcao", "Função"), mk("config", "Configurações"));
    let content;
    if (tab === "funcao") {
      const act = (label, name, on) => {
        const b = el("button", { className: "choice big" + (on ? " on" : ""), textContent: label });
        b.onclick = () => run(label, () => api("POST", `/api/actions/${name}`)).then(refreshHome);
        return b;
      };
      content = el("div", { className: "choices" },
        act("Aspirar pó", "dust", base.mode === 2),
        base.mode === 3 ? act("Encerrar lavagem", "stop-wash", true) : act("Limpar o esfregão", "wash-mop"),
        base.mode === 1 ? act("Encerrar secagem", "stop-dry", true) : act("Secar o esfregão", "dry"));
    } else {
      const wash = setting("2/61") ? "2/61" : "2/29";
      const group = (title, sub, keys) => {
        const det = el("details", { className: "set-group" });
        det.append(el("summary", {}, el("span", { textContent: title }), el("span", { className: "muted small", textContent: sub })));
        for (const k of keys) {
          const s = setting(k);
          if (s) det.append(el("div", { className: "set-row" }, el("span", { textContent: s.label }), control(s)));
        }
        return det;
      };
      const onoff = (k) => (V(k) ? "Ligada" : "Desligada");
      content = el("div", {},
        group("Coleta de pó", `${onoff("2/32")} · ${optLabel("2/33", V("2/33"))}`, ["2/32", "2/33", "2/82"]),
        group("Configurações de limpeza do esfregão", `${onoff("2/28")} · ${optLabel(wash, V(wash))}`, ["2/28", wash, "2/30", "2/84", "2/41"]),
        group("Configurações de secagem", `${onoff("2/34")} · ${optLabel("2/31", V("2/31"))}`, ["2/34", "2/31"]));
    }
    openSheet("Estação", [tabs, content]);
  }

  // controle de um ajuste (usado em Configurações e na Estação)
  function control(r) {
    const wrap = el("span", { className: "control" });
    const status = el("span", { className: "saving" });
    const save = async (value) => {
      status.className = "saving"; status.textContent = " salvando…";
      try {
        const res = await setProp(r.key, value);
        status.className = "ok"; status.textContent = res.code === 0 ? " ok" : ` código ${res.code}`;
        if (state) state.values[r.key] = res.value ?? value;
      } catch (e) { status.className = "err"; status.textContent = " " + e.message; }
    };
    let input;
    if (r.type === "bool") {
      input = el("input", { type: "checkbox", checked: !!r.value, className: "switch" });
      input.onchange = () => save(input.checked);
    } else if (r.type === "enum") {
      input = el("select");
      for (const [v, label] of r.options) input.append(el("option", { value: v, textContent: label, selected: v === r.value }));
      input.onchange = () => save(Number(input.value));
    } else if (r.type === "range") {
      const [lo, hi, step] = r.range;
      input = el("input", { type: "number", min: lo, max: hi, step: step || 1, value: r.value ?? "" });
      input.style.width = "7em";
      input.onchange = () => save(Number(input.value));
    } else if (r.type === "dnd") {
      input = el("input", { value: r.display || "", placeholder: "23:00-08:00", size: 12 });
      input.onchange = () => save(input.value);
    } else {
      input = el("input", { value: r.value ?? "" });
      input.onchange = () => save(input.value);
    }
    if (r.value === null || r.value === undefined) input.disabled = true;
    wrap.append(input, status);
    return wrap;
  }

  async function refreshHome() {
    await loadState();
    if (!settings) await loadSettings();
    renderHeader(); renderChips(); renderSubs(); renderStartButtons();
  }

  // ---- abas ---------------------------------------------------------------------------------
  const prevRaw = {};
  const views = {
    async inicio() {
      if (!robotId) return renderOnboarding();
      $("#onboarding").hidden = true;
      $(".home").hidden = false;
      await refreshHome();
      $("#btn-start").onclick = startPressed;
      $("#btn-home").onclick = () => run("Voltar à base", () => api("POST", "/api/actions/home"));
      $("#btn-stop").onclick = () => run("Encerrar", () => api("POST", "/api/actions/stop"));
      $("#btn-station").onclick = () => openStation();
      $("#btn-modes").onclick = openModes;
      $("#btn-shortcuts").onclick = openShortcuts;
      $("#btn-area").onclick = openArea;
      await Promise.all([renderHomeLists(), renderHomeMap()]);
    },

    async dashboards() {
      const range = views._range || (views._range = { days: 7, hours: 168 });
      for (const b of document.querySelectorAll("#dash-range button")) {
        b.classList.toggle("on", +b.dataset.days === range.days);
        b.onclick = () => { views._range = { days: +b.dataset.days, hours: +b.dataset.hours }; views.dashboards(); };
      }
      const body = $("#dash-body");
      body.classList.add("loading");    // mantém o quadro anterior enquanto recarrega
      const [c, t] = await Promise.all([
        api("GET", `/api/dash/cleanings?days=${range.days}`),
        api("GET", `/api/dash/timeline?hours=${range.hours}`),
      ]);
      body.classList.remove("loading");
      const n1 = (v, d = 1) => (v ?? 0).toLocaleString("pt-BR", { maximumFractionDigits: d });
      Charts.tiles($("#d-tiles"), [
        { label: "Limpezas", value: n1(c.totals.count, 0) },
        { label: "Área limpa", value: `${n1(c.totals.area_m2)} m²` },
        { label: "Tempo limpando", value: Charts.dur(0, c.totals.duration_min * 60000) },
        { label: "Média por limpeza", value: c.totals.avg_area_m2 == null ? "—" : `${n1(c.totals.avg_area_m2)} m²` },
      ]);
      const short = (d) => d.slice(8, 10) + "/" + d.slice(5, 7);
      Charts.columns($("#d-area"), c.by_day.map((d) => ({
        label: short(d.day), value: d.area_m2, tip: `${short(d.day)} · ${d.count} limpeza(s), ${n1(d.duration_min, 0)} min`,
      })), { format: (v) => n1(v), unit: " m²", labelEvery: Math.ceil(c.by_day.length / 10) });
      Charts.bars($("#d-rooms"), c.by_room.map((r) => ({ label: r.label, value: r.count })), { format: (v) => n1(v, 0) });
      api("GET", `/api/dash/coverage?days=${range.days}`).then(async (cv) => {
        const box = $("#d-coverage");
        if (!cv.cleanings || !cv.map) {
          box.textContent = "";
          $("#d-cov-note").textContent = cv.cleanings ? "Falta o mapa: abra o Início uma vez com a nuvem conectada."
            : "Ainda sem caminhos gravados neste período. O app grava o caminho de cada limpeza feita com ele aberto e a nuvem conectada.";
          return;
        }
        await MapView.render(box, { ...cv.map, _grid: undefined, path: [], robot: null }, { heat: cv, dim: true });
        $("#d-cov-note").textContent = `${cv.cleanings} limpeza(s). Quanto mais escuro, mais limpezas passaram pelo trecho (máximo ${cv.max}).`;
      }).catch(() => {});
      Charts.bars($("#d-types"), c.by_type.map((r) => ({ label: r.label, value: r.count })), { format: (v) => n1(v, 0) });
      Charts.heat($("#d-heat"), c.heat.matrix, c.heat.weekdays);

      const colorVars = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s-other"];
      const colors = Object.fromEntries(t.groups.map((g, i) => [g, `var(${colorVars[i]})`]));
      const tg = t.time_by_group, ms = (x) => x * 1000;
      const tracked = Object.values(tg).reduce((a, b) => a + b, 0);
      Charts.tiles($("#d-tiles2"), [
        { label: "Limpando", value: Charts.dur(0, ms(tg["Limpando"])) },
        { label: "Base trabalhando", value: Charts.dur(0, ms(tg["Base trabalhando"])) },
        { label: "Na base", value: tracked ? `${n1((100 * tg["Na base"]) / tracked, 0)} %` : "—", sub: "do tempo acompanhado" },
        { label: "Bateria agora", value: V("3/1") != null ? `${V("3/1")} %` : "—" },
      ]);
      // eixo começa onde há dados: uma janela de 7 dias com 3 h de dados ficaria espremida na borda
      const x0 = t.data_from && new Date(t.data_from) > new Date(t.start) ? t.data_from : t.start;
      Charts.band($("#d-band"), t.segments, t.groups, { x0, x1: t.end, colors });
      Charts.line($("#d-battery"), t.battery, { x0, x1: t.end, yMax: 100, unit: " %", name: "bateria" });
      $("#d-note").textContent = t.data_from && new Date(t.data_from) > new Date(t.start)
        ? `Dados desde ${new Date(t.data_from).toLocaleString("pt-BR")} (antes disso o app não estava gravando).` : "";
    },

    async historico() {
      const f = new FormData($("#hist-filter"));
      const q = new URLSearchParams([...f].filter(([, v]) => v));
      const data = await api("GET", `/api/history?${q}`);
      Table.render($("#t-history"), [
        { key: "start", label: "Início", render: (r) => fmtDate(r.start), sortValue: (r) => r.stime },
        { key: "type_label", label: "Tipo" },
        { key: "room_names", label: "Cômodos / rotina", render: (r) => r.routine ? (r.raw_label?.startsWith("zones:") ? r.routine : `Rotina "${r.routine}"`) : r.room_names.join(", ") || "—" },
        { key: "duration_s", label: "Duração", num: true, render: (r) => fmtDur(r.duration_s) },
        { key: "area_m2", label: "Área (m²)", num: true, render: (r) => r.area_m2?.toLocaleString("pt-BR") },
        { key: "source", label: "Fonte", render: (r) => r.source === "robot" ? "robô (nuvem)" : "app" },
        { key: "has_path", label: "Caminho", filter: false, sortValue: (r) => r.has_path ? 1 : 0,
          render: (r) => { if (!r.has_path) return el("span", { className: "muted", textContent: "—" });
            const b = el("button", { textContent: "Ver caminho" }); b.onclick = () => showPath(r); return b; } },
      ], data.records, {
        sort: "start", desc: true,
        footer: (rows) => ({
          start: `${rows.length} limpezas`,
          duration_s: fmtDur(rows.reduce((a, r) => a + (r.duration_s || 0), 0)),
          area_m2: rows.reduce((a, r) => a + (r.area_m2 || 0), 0).toLocaleString("pt-BR", { maximumFractionDigits: 2 }),
        }),
      });
    },

    async consumiveis() {
      const items = await api("GET", "/api/consumables");
      Table.render($("#t-consumables"), [
        { key: "name", label: "Item" },
        { key: "pct", label: "Vida útil", num: true, render: (r) => r.pct == null ? "—" : `${r.pct} %` },
        { key: "hours", label: "Horas restantes", num: true },
        { key: "forecast", label: "Previsão de troca", sortValue: (r) => r.forecast ? r.forecast.days_left : 1e9,
          render: (r) => r.forecast ? `em ~${r.forecast.days_left} dia(s) (${new Date(r.forecast.date + "T12:00").toLocaleDateString("pt-BR")})` : "—" },
        { key: "reset", label: "", filter: false, sort: false, render: (r) => {
          const b = el("button", { textContent: "Zerar" });
          b.onclick = () => confirmAction(`Marcar ${r.name} como trocado (volta a 100 %)?`,
            () => run(`Zerar ${r.name}`, async () => { await api("POST", `/api/consumables/${r.item}/reset`); views.consumiveis(); }));
          return b;
        } },
      ], items, { rowClass: (r) => r.alert ? "alert" : "", filters: false });
    },

    async configuracoes() {
      window.H50?.renderNotify?.();
      const host = $("#t-settings");
      if (host.contains(document.activeElement) && document.activeElement.tagName !== "BODY") return;
      const items = await loadSettings();
      Table.render(host, [
        { key: "label", label: "Ajuste" },
        { key: "control", label: "Valor", filter: false, sort: false, render: control },
        { key: "key", label: "siid/piid" },
      ], items, { group: (r) => r.group });
    },

    async diagnostico() {
      await loadState();
      const flash = new Set();
      for (const r of state.rows) {
        const v = JSON.stringify(r.raw);
        if (r.key in prevRaw && prevRaw[r.key] !== v) flash.add(r.key);
        prevRaw[r.key] = v;
      }
      Table.render($("#t-state"), [
        { key: "label", label: "Propriedade" },
        { key: "value", label: "Valor", render: (r) => r.value ?? "—" },
        { key: "key", label: "siid/piid" },
        { key: "changed_at", label: "Mudou", render: (r) => ago(r.changed_at), sortValue: (r) => r.changed_at || "", filter: false },
      ], state.rows, { rowClass: (r) => flash.has(r.key) ? "flash" : "" });

      const [faults, cmds, rooms, furniture, cst] = await Promise.all([api("GET", "/api/faults"), api("GET", "/api/commands"),
        api("GET", "/api/rooms"), api("GET", "/api/furniture"), api("GET", "/api/cloud/status").catch(() => ({}))]);
      $("#cloud-info").textContent = cst.state === "ok" ? `Conectado: conta ${cst.user_id}, servidor ${cst.country || "?"}` : "Não conectado (conecte pela tela Início)";
      $("#map-fetch").onclick = () => run("Mapa", async () => { await api("POST", "/api/cloud/map"); mapData = null; });
      $("#cloud-logout").onclick = () => run("Sair", async () => { await api("POST", "/api/cloud/logout"); mapData = null; views.diagnostico(); });
      Table.render($("#t-faults"), [
        { key: "ts", label: "Início", render: (r) => fmtDate(r.ts) },
        { key: "code", label: "Código", num: true },
        { key: "cleared", label: "Resolvida", render: (r) => r.cleared ? fmtDate(r.cleared) : "ativa" },
      ], faults);
      Table.render($("#t-commands"), [
        { key: "ts", label: "Quando", render: (r) => fmtDate(r.ts), sortValue: (r) => r.ts },
        { key: "command", label: "Comando" },
        { key: "params", label: "Parâmetros" },
        { key: "result_code", label: "Código" },
        { key: "latency_ms", label: "ms", num: true },
        { key: "error", label: "Erro" },
        { key: "source", label: "Origem" },
      ], cmds);
      Table.render($("#t-rooms"), [
        { key: "id", label: "id", num: true },
        { key: "name", label: "Cômodo" },
        { key: "last_clean", label: "Última limpeza", render: (r) => fmtDate(r.last_clean) },
      ], rooms, { filters: false });
      Table.render($("#t-furniture"), [
        { key: "name", label: "Móvel" },
        { key: "type", label: "Código", num: true },
        { key: "room", label: "Cômodo" },
      ], furniture, { filters: false });
    },
  };

  // ---- navegação --------------------------------------------------------------------------------
  const current = () => (location.hash || "#inicio").slice(1);
  async function show() {
    const tab = views[current()] ? current() : "inicio";
    for (const s of document.querySelectorAll("main > section")) s.hidden = s.id !== tab;
    for (const a of document.querySelectorAll("#tabs a")) {
      const on = a.getAttribute("href") === `#${tab}`;
      a.classList.toggle("on", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    try { await views[tab](); } catch (e) { toast(e.message, true); }
  }
  let pending;
  const refresh = () => { clearTimeout(pending); pending = setTimeout(show, 300); };
  window.addEventListener("hashchange", show);

  // ---- tempo real (SSE) ---------------------------------------------------------------------------
  let streams = [];
  function connect() {
    for (const s of streams) s.close();
    streams = [];
    const acc = new EventSource("/api/stream");              // eventos da conta (nuvem, robôs)
    streams.push(acc);
    acc.addEventListener("cloud", () => { if (current() === "inicio") renderHomeMap(); else if (current() === "robos") refresh(); });
    acc.addEventListener("robots", async () => { await reloadRobots(); if (current() === "robos") refresh(); });
    if (!robotId) return;
    const es = new EventSource(`/api/r/${robotId}/stream`);  // eventos do robô ativo
    streams.push(es);
    es.addEventListener("state", async (e) => {
      const d = JSON.parse(e.data || "{}");
      // durante a limpeza o 10/1 muda a cada ~1 min: com o Início aberto, baixa o mapa novo
      if (current() === "inicio" && cloud?.state === "ok" && d.changes && "10/1" in d.changes)
        api("POST", "/api/cloud/map").then(async () => { mapData = await api("GET", "/api/map"); drawMap(); }).catch(() => {});
      // aplica as mudanças direto (sem rebuscar o estado inteiro a cada 5 s durante a limpeza);
      // só rebusca quando muda algo que o servidor rotula (status, base) ou a cada 15 s
      const ch = d.changes || {};
      if (state) Object.assign(state.values, ch);
      if (!state || "2/2" in ch || "2/18" in ch || Date.now() - (state._at || 0) > 15000) {
        await loadState().catch(() => {});
        if (state) state._at = Date.now();
      }
      renderHeader();
      const touchesSettings = settings && Object.keys(ch).some((k) => settings.some((x) => x.key === k));
      if (touchesSettings) loadSettings().then(() => current() === "inicio" && renderSubs()).catch(() => {});
      if (current() === "inicio") { renderChips(); renderSubs(); renderStartButtons(); }
      else if (current() === "diagnostico") refresh();
      else if (current() === "configuracoes" && touchesSettings) refresh();   // mudou pelo app oficial
    });
    es.addEventListener("mapdata", async () => { mapData = await api("GET", "/api/map").catch(() => mapData); if (current() === "inicio" && mapData) drawMap(); });
    for (const ev of ["online", "offline"]) es.addEventListener(ev, async () => { await loadState().catch(() => {}); renderHeader(); });
    es.addEventListener("command", () => { if (current() === "diagnostico") refresh(); });
    es.addEventListener("cleaning", () => { if (["historico", "dashboards", "inicio"].includes(current())) refresh(); });
    es.addEventListener("fault", () => { if (current() === "diagnostico") refresh(); });
    es.addEventListener("map", () => {
      const b = $("#banner");
      b.textContent = "O mapa foi editado no app da Xiaomi (paredes, zonas ou cômodos).";
      b.hidden = false;
      api("POST", "/api/cloud/map").then(async () => { mapData = await api("GET", "/api/map"); if (current() === "inicio") drawMap(); }).catch(() => {});
    });
    es.onerror = () => { $("#dot").className = "dot off"; $("#conn-sub").textContent = "sem conexão com o servidor…"; };
  }

  // ---- início --------------------------------------------------------------------------------------
  // ---- robôs ------------------------------------------------------------------------------------
  function renderRobotSwitcher() {
    const cur = robots.find((r) => r.id === robotId);
    $("#name").textContent = cur ? cur.name : "H50 Console";
    document.title = `${cur ? cur.name + " · " : ""}H50 Console`;
    const sel = $("#robot-select");
    sel.hidden = robots.length < 2;
    $("#name").hidden = robots.length >= 2;
    sel.replaceChildren(...robots.map((r) => el("option", { value: r.id, textContent: r.name, selected: r.id === robotId })));
    sel.onchange = () => setRobot(sel.value);
  }
  async function reloadRobots() {
    robots = await api("GET", "/api/robots");
    if (!robots.some((r) => r.id === robotId)) {
      robotId = robots[0]?.id ?? null;
      resetCaches();
      connect();
    }
    renderRobotSwitcher();
    return robots;
  }
  function resetCaches() {
    state = null; settings = null; mapData = null; roomsCache = null; areaSel.clear();
    for (const k of Object.keys(prevRaw)) delete prevRaw[k];
  }
  function setRobot(id) {
    robotId = id;
    try { localStorage.setItem("h50.robot", id); } catch { /* sem armazenamento: tudo bem */ }
    resetCaches();
    renderRobotSwitcher();
    connect();
    show();
  }
  function renderOnboarding() {
    $(".home").hidden = true;
    const box = $("#onboarding");
    box.hidden = false;
    const go = el("button", { className: "primary", textContent: "Conectar conta e adicionar robô" });
    go.onclick = () => { location.hash = "#robos"; };
    box.replaceChildren(
      el("h2", { textContent: "Vamos configurar" }),
      el("p", { className: "muted", textContent: "Conecte sua conta Xiaomi: o app lista seus robôs, pega o token sozinho e acha o robô na rede mesmo que o IP mude. Também dá para informar IP e token à mão." }),
      el("div", { className: "bar" }, go));
  }

  async function init() {
    appInfo = await api("GET", "/api/app");
    robots = appInfo.robots || [];
    let saved = null;
    try { saved = localStorage.getItem("h50.robot"); } catch { /* ignora */ }
    robotId = (robots.find((r) => r.id === saved) || robots[0])?.id ?? null;
    renderRobotSwitcher();
    $("#name").onclick = () => {
      const cur = robots.find((r) => r.id === robotId);
      if (!cur) return;
      const inp = el("input", { value: cur.name, maxLength: 40 });
      openSheet("Nome do robô", [inp, el("p", { className: "muted small", textContent: "Vale só nesta réplica; o app Xiaomi Home mantém o nome dele." })], {
        confirm: () => run("Nome", async () => { await api("PUT", "/api/app/name", { value: inp.value }); await reloadRobots(); }),
      });
    };
    const types = { 1: "Casa toda", 2: "Zona", 3: "Cômodos", 6: "Ponto", 7: "Personalizada", 10: "Mapeamento" };
    const sel = $("#hist-filter select[name=type]");
    for (const [v, l] of Object.entries(types)) sel.append(el("option", { value: v, textContent: l }));
    $("#hist-filter").onsubmit = (e) => { e.preventDefault(); views.historico(); };
    $("#hist-sync").onclick = () => run("Histórico", async () => {
      const r = await api("POST", "/api/cloud/history");
      toast(`Histórico: ${r.records} registros, ${r.added} novos`);
      await views.historico();
    });

    // segurança: senha e sessões
    const auth = await fetch("/api/auth/status").then((r) => r.json()).catch(() => ({ enabled: false }));
    $("#sec-card").hidden = !auth.enabled;
    document.body.classList.toggle("viewer", auth.role === "viewer");
    if (auth.role === "viewer") $("#sec-note").textContent = "Você entrou com o perfil somente leitura.";
    const viewerCard = $("#viewer-card");
    viewerCard.hidden = !(auth.enabled && auth.role === "admin");
    const viewerShow = (on) => { $("#viewer-state").textContent = on ? "Ligado" : "Desligado"; $("#viewer-off").hidden = !on; };
    viewerShow(auth.viewer_enabled);
    $("#viewer-set").onclick = () => {
      const pw = el("input", { type: "password", placeholder: "Senha do visualizador (mínimo 8)", autocomplete: "new-password" });
      openSheet("Senha do visualizador", [el("form", { className: "form-grid", onsubmit: (e) => e.preventDefault() }, pw,
        el("p", { className: "muted small", textContent: "Trocar a senha desconecta quem já entrou com o perfil. Use uma senha diferente da sua." }))], {
        confirmLabel: "Salvar",
        confirm: async () => { await api("POST", "/api/auth/viewer", { password: pw.value }); viewerShow(true); toast("Perfil somente leitura ligado"); },
      });
    };
    $("#viewer-off").onclick = () => confirmAction("Desligar o perfil somente leitura? Quem estiver conectado com ele sai.",
      () => run("Desligar", async () => { await api("POST", "/api/auth/viewer", { password: "" }); viewerShow(false); }));
    $("#sec-pw").onclick = () => {
      const cur = el("input", { type: "password", placeholder: "Senha atual", autocomplete: "current-password" });
      const nw = el("input", { type: "password", placeholder: "Nova senha (mínimo 8 caracteres)", autocomplete: "new-password" });
      openSheet("Alterar senha", [el("form", { className: "form-grid", onsubmit: (e) => e.preventDefault() }, cur, nw,
        el("p", { className: "muted small", textContent: "Os outros aparelhos conectados precisarão entrar de novo." }))], {
        confirmLabel: "Alterar",
        confirm: async () => { await api("POST", "/api/auth/password", { current: cur.value, new: nw.value }); toast("Senha alterada"); },
      });
    };
    $("#sec-out").onclick = async () => { await api("POST", "/api/auth/logout", {}).catch(() => {}); location.href = "/login.html"; };
    $("#sec-all").onclick = () => confirmAction("Sair de todos os aparelhos? Este também continua conectado.",
      () => run("Sair de todos", () => api("POST", "/api/auth/revoke", {})));

    if (appInfo.raw_enabled) {
      $("#raw-box").hidden = false;
      $("#raw-form").onsubmit = async (e) => {
        e.preventDefault();
        const f = Object.fromEntries(new FormData(e.target));
        const body = f.op === "get" ? { keys: f.key.split(",").map((s) => s.trim()) }
          : f.op === "set" ? { key: f.key.trim(), value: parse(f.value, f.value) }
          : { key: f.key.trim(), params: f.value ? parse(f.value, []) : [] };
        try { $("#raw-out").textContent = JSON.stringify(await api("POST", `/api/raw/${f.op}`, body), null, 2); }
        catch (err) { $("#raw-out").textContent = err.message; }
      };
    }
    if (location.hash === "#painel" || location.hash === "#mapa") location.hash = "#inicio";
    if (!robotId && !location.hash) location.hash = "#robos";
    if (robotId) await loadState().catch(() => null);
    renderHeader();
    await show();
    const qs = new URLSearchParams(location.search);
    // ?sheet=modos|atalhos|estacao|area abre o menu direto (útil para capturas de tela headless)
    const sheets = { modos: openModes, atalhos: openShortcuts, estacao: () => openStation(qs.get("tab") || "funcao"), area: openArea };
    if (sheets[qs.get("sheet")]) await sheets[qs.get("sheet")]();
    if (!qs.has("static")) {
      connect();
      if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => { /* sem HTTPS: tudo bem */ });
    }
    setInterval(renderHeader, 10000);   // atualiza o "há X s"
  }
  // espaço de nomes para as telas em outros arquivos (robots.js)
  window.H50 = {
    $, el, api, download, toast, run, openSheet, confirmAction, chip, views, fmtDate, ago, reloadRobots, setRobot,
    get robotId() { return robotId; }, get robots() { return robots; }, get rawEnabled() { return !!appInfo?.raw_enabled; },
  };
  window.addEventListener("load", init);      // depois de todos os scripts terem registrado suas telas
})();

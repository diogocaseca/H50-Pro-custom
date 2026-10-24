// Desenho do mapa (canvas, JS puro). Entrada: /api/map (mapdecode.for_ui).
// Mundo em mm, x para a direita e y para cima; grade com linha 0 = menor y (por isso a inversão).
(function () {
  const FURN = { 1220: "Sofá", 1400: "Mesa", 1740: "Liteira" };
  const ROOM_VARS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s-other"];

  async function inflate(b64) {
    const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const stream = new Blob([bin]).stream().pipeThrough(new DecompressionStream("deflate"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  function cssColor(name) {
    const probe = document.createElement("span");
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    const m = c.match(/\d+(\.\d+)?/g).map(Number);
    return m.slice(0, 3);
  }

  async function render(host, m, { onHover, onRoomClick, selected, heat, dim, onPoint, marks, zones, onDraw } = {}) {
    const grid = m._grid || (m._grid = await inflate(m.grid_zlib_b64));
    const sel = selected && selected.size ? selected : null;   // com seleção, o resto fica apagado
    const faint = dim && !sel;                                  // mapa de calor: cômodos quase apagados
    const W = m.width, H = m.height, res = m.resolution, [ox, oy] = m.origin;

    // recorte: caixa dos pixels não vazios + margem
    let c0 = W, c1 = 0, r0 = H, r1 = 0;
    for (let r = 0; r < H; r++) for (let c = 0; c < W; c++) if (grid[r * W + c]) {
      if (c < c0) c0 = c; if (c > c1) c1 = c; if (r < r0) r0 = r; if (r > r1) r1 = r;
    }
    const pad = 6;
    c0 = Math.max(0, c0 - pad); r0 = Math.max(0, r0 - pad); c1 = Math.min(W - 1, c1 + pad); r1 = Math.min(H - 1, r1 + pad);
    const cw = c1 - c0 + 1, ch = r1 - r0 + 1;

    // cores: cômodos pela ordem da paleta (identidade), parede em tinta de texto
    const roomIds = m.rooms.map((r) => r.id).sort((a, b) => a - b);
    const roomRGB = Object.fromEntries(roomIds.map((id, i) => [id, cssColor(ROOM_VARS[i % ROOM_VARS.length])]));
    const wallRGB = cssColor("--text"), floorRGB = cssColor("--muted");
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;

    // grade em resolução nativa (1 px = 50 mm), invertendo as linhas
    const off = document.createElement("canvas");
    off.width = cw; off.height = ch;
    const img = off.getContext("2d").createImageData(cw, ch);
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
      const v = grid[r * W + c];
      if (!v) continue;
      const i = ((r1 - r) * cw + (c - c0)) * 4;
      const rgb = v === 1 ? wallRGB : roomRGB[v] || floorRGB;
      const a = v === 1 ? 230 : sel ? (sel.has(v) ? (dark ? 200 : 170) : 35) : faint ? 28 : dark ? 120 : 90;
      img.data.set([rgb[0], rgb[1], rgb[2], a], i);
    }
    off.getContext("2d").putImageData(img, 0, 0);

    // tela: escala para caber na largura
    const box = host.clientWidth || 800;
    const maxH = Math.max(280, window.innerHeight * (host.dataset.maxh ? +host.dataset.maxh : 0.72));
    const scale = Math.max(1, Math.min(6, (box - 2) / cw, maxH / ch));
    const dpr = window.devicePixelRatio || 1;
    const cv = document.createElement("canvas");
    cv.style.width = `${cw * scale}px`; cv.style.height = `${ch * scale}px`;
    cv.width = Math.round(cw * scale * dpr); cv.height = Math.round(ch * scale * dpr);
    cv.setAttribute("role", "img");
    cv.setAttribute("aria-label", `Mapa da casa com ${m.rooms.length} cômodo(s): ${m.rooms.map((r) => r.name).filter(Boolean).join(", ")}`);
    const g = cv.getContext("2d");
    g.scale(dpr, dpr);
    g.imageSmoothingEnabled = false;
    g.drawImage(off, 0, 0, cw * scale, ch * scale);

    const X = (x) => ((x - ox) / res - c0 + 0.5) * scale;
    const Y = (y) => (r1 - (y - oy) / res + 0.5) * scale;
    const ink = `rgb(${wallRGB.join(",")})`;

    // mapa de calor: quantas limpezas passaram por cada quadrado (um tom só, mais escuro = mais vezes)
    if (heat && heat.cells.length) {
      const [hr, hg, hb] = cssColor("--s1");
      for (const [cx, cy, n] of heat.cells) {
        g.fillStyle = `rgba(${hr},${hg},${hb},${(0.15 + 0.7 * (n / heat.max)).toFixed(2)})`;
        const x0 = X(cx), y0 = Y(cy + heat.cell);
        g.fillRect(x0, y0, X(cx + heat.cell) - x0 + 0.5, Y(cy) - y0 + 0.5);
      }
    }
    // trajetória
    if (m.path.length > 1) {
      // tipo 1 = limpando, 0 = deslocamento; o caminho da base até a área vem como um salto sem pontos
      // intermediários: quebra a linha em saltos > 1 m para não riscar o mapa em diagonal
      g.beginPath();
      let prev = null;
      for (const [x, y] of m.path) {
        if (prev && Math.hypot(x - prev[0], y - prev[1]) <= 1000) g.lineTo(X(x), Y(y));
        else g.moveTo(X(x), Y(y));
        prev = [x, y];
      }
      g.strokeStyle = dark ? "rgba(240,239,236,.55)" : "rgba(11,11,11,.45)";
      g.lineWidth = 1; g.lineJoin = g.lineCap = "round"; g.stroke();
    }
    // móveis
    g.font = "11px system-ui, sans-serif";
    for (const f of m.furniture) {
      const p = f.pos;
      g.beginPath();
      for (let i = 0; i < p.length; i += 2) (i ? g.lineTo : g.moveTo).call(g, X(p[i]), Y(p[i + 1]));
      g.closePath();
      g.fillStyle = dark ? "rgba(255,255,255,.18)" : "rgba(255,255,255,.75)"; g.fill();
      g.strokeStyle = ink; g.lineWidth = 1; g.stroke();
      const cx = (X(p[0]) + X(p[4])) / 2, cy = (Y(p[1]) + Y(p[5])) / 2;
      label(g, FURN[f.type] || `móvel ${f.type}`, cx, cy, ink, dark, 11);
    }
    // paredes virtuais (vermelho reservado para "proibido")
    g.strokeStyle = "#d03b3b"; g.lineWidth = 3; g.lineCap = "round";
    for (const [x1, y1, x2, y2] of m.walls) { g.beginPath(); g.moveTo(X(x1), Y(y1)); g.lineTo(X(x2), Y(y2)); g.stroke(); }
    // nomes dos cômodos
    for (const r of m.rooms) if (r.pos[0] != null) label(g, r.name, X(r.pos[0]), Y(r.pos[1]), ink, dark, 13, true);
    // base e robô
    if (m.pile) {
      const [x, y] = m.pile, s = Math.max(8, scale * 5);
      g.fillStyle = dark ? "#f0efec" : "#0b0b0b";
      g.fillRect(X(x) - s / 2, Y(y) - s / 2, s, s);
      label(g, "base", X(x), Y(y) - s, ink, dark, 10);
    }
    if (m.robot) {
      const [x, y] = m.robot, r = Math.max(6, scale * 3.5);
      g.beginPath(); g.arc(X(x), Y(y), r + 2, 0, 2 * Math.PI); g.fillStyle = dark ? "#1c2028" : "#fff"; g.fill();
      g.beginPath(); g.arc(X(x), Y(y), r, 0, 2 * Math.PI); g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent"); g.fill();
    }

    // zonas de limpeza (mm, [x1,y1,x2,y2]): retângulo na cor de destaque, numerado, com o tamanho em metros
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2f6fde";
    const zoneRect = (ctx, [x1, y1, x2, y2], text) => {
      const l = X(Math.min(x1, x2)), r = X(Math.max(x1, x2)), t = Y(Math.max(y1, y2)), b = Y(Math.min(y1, y2));
      ctx.save();
      ctx.fillStyle = accent; ctx.globalAlpha = 0.22; ctx.fillRect(l, t, r - l, b - t);
      ctx.globalAlpha = 1; ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); ctx.strokeRect(l, t, r - l, b - t);
      ctx.restore();
      if (text) label(ctx, text, (l + r) / 2, (t + b) / 2, ink, dark, 11, true);
    };
    const size = ([x1, y1, x2, y2]) => `${(Math.abs(x2 - x1) / 1000).toFixed(1).replace(".", ",")} × ${(Math.abs(y2 - y1) / 1000).toFixed(1).replace(".", ",")} m`;
    (zones || []).forEach((z, i) => zoneRect(g, z, `${i + 1} · ${size(z)}`));

    // marcadores do editor (mm): círculo com cruz
    for (const [x, y] of marks || []) {
      g.beginPath(); g.arc(X(x), Y(y), 6, 0, 2 * Math.PI);
      g.fillStyle = "rgba(47,111,222,.35)"; g.fill(); g.strokeStyle = "#2f6fde"; g.lineWidth = 2; g.stroke();
    }

    // tooltip: cômodo sob o cursor
    const names = Object.fromEntries(m.rooms.map((r) => [r.id, r.name]));
    cv.addEventListener("pointermove", (e) => {
      const b = cv.getBoundingClientRect();
      const c = Math.floor((e.clientX - b.left) / scale) + c0, r = r1 - Math.floor((e.clientY - b.top) / scale);
      const v = c >= 0 && c < W && r >= 0 && r < H ? grid[r * W + c] : 0;
      const xm = ox + c * res, ym = oy + r * res;
      onHover && onHover(e, v === 1 ? "parede" : names[v] || null, xm, ym);
    });
    cv.addEventListener("pointerleave", () => onHover && onHover(null));
    if (onPoint) {      // editor: o clique devolve o ponto em mm (mundo), com precisão menor que o pixel da grade
      cv.style.cursor = "crosshair";
      cv.addEventListener("click", (e) => {
        const b = cv.getBoundingClientRect();
        const px = e.clientX - b.left, py = e.clientY - b.top;
        onPoint(Math.round(ox + (px / scale + c0 - 0.5) * res), Math.round(oy + (r1 + 0.5 - py / scale) * res), e);
      });
    }
    if (onRoomClick) {
      cv.style.cursor = "pointer";
      cv.addEventListener("click", (e) => {
        const b = cv.getBoundingClientRect();
        const c = Math.floor((e.clientX - b.left) / scale) + c0, r = r1 - Math.floor((e.clientY - b.top) / scale);
        const v = c >= 0 && c < W && r >= 0 && r < H ? grid[r * W + c] : 0;
        if (names[v]) onRoomClick(v);
      });
    }

    const legend = document.createElement("div");
    legend.className = "viz-legend";
    const item = (color, text, line) => {
      const i = document.createElement("div"); i.className = "viz-legend-item";
      const k = document.createElement("div"); k.className = "viz-swatch"; k.style.background = color;
      if (line) { k.style.height = "3px"; }
      i.append(k, document.createTextNode(text)); legend.append(i);
    };
    m.rooms.slice().sort((a, b) => a.id - b.id).forEach((r) => item(`rgb(${roomRGB[r.id].join(",")})`, r.name));
    item("#d03b3b", "parede virtual", true);
    if (m.path.length) item(dark ? "rgba(240,239,236,.55)" : "rgba(11,11,11,.45)", "trajetória", true);

    const wrap = document.createElement("div");
    wrap.className = "map-wrap";
    const holder = document.createElement("div");
    holder.style.position = "relative";
    holder.append(cv);
    wrap.append(holder);

    // desenhar zona: arrastar na tela (dedo ou mouse) faz um retângulo; ao soltar, devolve [x1,y1,x2,y2] em mm
    if (onDraw) {
      const over = document.createElement("canvas");
      over.style.cssText = `position:absolute;left:0;top:0;width:${cv.style.width};height:${cv.style.height};pointer-events:none`;
      over.width = cv.width; over.height = cv.height;
      const og = over.getContext("2d");
      og.scale(dpr, dpr);
      holder.append(over);
      cv.style.cursor = "crosshair";
      const toMm = (px, py) => [Math.round(ox + (px / scale + c0 - 0.5) * res), Math.round(oy + (r1 + 0.5 - py / scale) * res)];
      const at = (e) => { const b = cv.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
      let start = null;
      cv.addEventListener("pointerdown", (e) => { start = at(e); cv.setPointerCapture(e.pointerId); e.preventDefault(); });
      cv.addEventListener("pointermove", (e) => {
        if (!start) return;
        const [px, py] = at(e);
        og.clearRect(0, 0, cv.width, cv.height);
        zoneRect(og, [...toMm(...start), ...toMm(px, py)], size([...toMm(...start), ...toMm(px, py)]));
      });
      const finish = (e) => {
        if (!start) return;
        const [px, py] = at(e), [sx, sy] = start;
        start = null;
        og.clearRect(0, 0, cv.width, cv.height);
        if (Math.abs(px - sx) < 8 || Math.abs(py - sy) < 8) return;     // toque sem arrastar
        onDraw([...toMm(sx, sy), ...toMm(px, py)]);
      };
      cv.addEventListener("pointerup", finish);
      cv.addEventListener("pointercancel", () => { start = null; og.clearRect(0, 0, cv.width, cv.height); });
    }
    host.replaceChildren(wrap, legend);
    return { area_m2: (grid.filter((v) => v > 1).length * res * res) / 1e6 };
  }

  function label(g, text, x, y, ink, dark, size, bold) {
    g.font = `${bold ? "600 " : ""}${size}px system-ui, sans-serif`;
    g.textAlign = "center"; g.textBaseline = "middle";
    g.lineWidth = 3; g.strokeStyle = dark ? "rgba(28,32,40,.85)" : "rgba(255,255,255,.9)";
    g.strokeText(text, x, y);
    g.fillStyle = ink; g.fillText(text, x, y);
  }

  window.MapView = { render };
})();

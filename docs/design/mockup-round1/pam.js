// PAM mockup shared data + arrangement canvas. Same example data for every option.
// Real data from KEVIN-PC-1 on 2026-11-05 (gdctl, wpctl). Marked illustrative where faked.
const SCREENS = {
  lenL: { name: "Lenovo LI2364d", sub: "23\" · left twin (lower port)", w: 1920, h: 1080, connected: true },
  rog:  { name: "ASUS ROG PG248Q", sub: "24\" · serial L8LMQS075392", w: 1920, h: 1080, connected: true },
  lenR: { name: "Lenovo LI2364d", sub: "23\" · right twin (higher port)", w: 1920, h: 1080, connected: true },
  proj: { name: "SANTAK S2-TEK TV", sub: "42\" projector · not connected (illustrative)", w: 1920, h: 1080, connected: false },
};
const OUTPUTS = ["Audio Adapter (Unitek Y-247A) Analog Stereo", "GA102 HDMI 2 (NVIDIA)", "Starship/Matisse Digital (S/PDIF)"];
const INPUTS = ["NexiGo N60 FHD Webcam Mono", "Audio Adapter (Unitek Y-247A) Mono", "Starship/Matisse Analog Stereo"];
const PROFILES = [
  { id: "desk", name: "Desk — 3 monitors", key: "Super+F1", out: 0, inp: 0, primary: "rog",
    layout: { lenL: [0, 0], rog: [1920, 0], lenR: [3840, 0] } },
  { id: "center", name: "Center only", key: "Super+F2", out: 0, inp: 0, primary: "rog",
    layout: { rog: [0, 0] } },
  { id: "proj", name: "Projector", key: "Super+F3", out: 1, inp: 0, primary: "proj",
    layout: { proj: [0, 0] } },
];
const state = { profile: 0, selected: "rog", listeners: [] };
const P = () => PROFILES[state.profile];
const emit = () => state.listeners.forEach((f) => f());
const onChange = (f) => state.listeners.push(f);
function selectProfile(i) { state.profile = i; state.selected = Object.keys(P().layout)[0]; emit(); }
function toggleScreen(id) {
  const L = P().layout;
  if (L[id]) { delete L[id]; if (P().primary === id) P().primary = Object.keys(L)[0] || null; }
  else { const xs = Object.entries(L).map(([k, [x]]) => x + SCREENS[k].w); L[id] = [xs.length ? Math.max(...xs) : 0, 0]; }
  state.selected = id; emit();
}
function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.append(t); }
  t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 2200);
}
function applyProfile() { toast(`Applied “${P().name}” — screens and audio switched (mockup)`); }

// Arrangement canvas: drag screens, snap to neighbours' edges, click to select.
function canvas(el, { showUnused = true } = {}) {
  el.classList.add("canvas");
  function draw() {
    el.innerHTML = "";
    const L = P().layout, ids = Object.keys(L);
    const all = ids.map((k) => [L[k][0], L[k][1], SCREENS[k].w, SCREENS[k].h]);
    const minX = Math.min(0, ...all.map((a) => a[0])), minY = Math.min(0, ...all.map((a) => a[1]));
    const maxX = Math.max(1920, ...all.map((a) => a[0] + a[2])), maxY = Math.max(1080, ...all.map((a) => a[1] + a[3]));
    const W = el.clientWidth - 48, H = el.clientHeight - 48;
    const s = Math.min(W / (maxX - minX), H / (maxY - minY), 0.12);
    const ox = 24 + (W - (maxX - minX) * s) / 2 - minX * s, oy = 24 + (H - (maxY - minY) * s) / 2 - minY * s;
    if (!ids.length) el.innerHTML = '<div class="empty">No screens in this profile — add one below</div>';
    for (const k of ids) {
      const d = document.createElement("div"), S = SCREENS[k];
      d.className = "scr" + (k === state.selected ? " sel" : "") + (S.connected ? "" : " gone");
      Object.assign(d.style, { left: ox + L[k][0] * s + "px", top: oy + L[k][1] * s + "px", width: S.w * s + "px", height: S.h * s + "px" });
      d.innerHTML = `<b>${S.name}</b><span>${S.connected ? "1920×1080 · 60 Hz" : "not connected"}</span>${P().primary === k ? '<i class="star">★ primary</i>' : ""}`;
      d.onpointerdown = (e) => {
        state.selected = k; d.setPointerCapture(e.pointerId);
        const sx = e.clientX, sy = e.clientY, [x0, y0] = L[k];
        document.querySelectorAll(".scr.sel").forEach((n) => n.classList.remove("sel")); d.classList.add("sel");
        d.onpointermove = (m) => {
          let nx = x0 + (m.clientX - sx) / s, ny = y0 + (m.clientY - sy) / s;
          for (const o of ids) if (o !== k) {
            const [qx, qy] = L[o], snap = 120;
            for (const [a, b] of [[nx, qx + SCREENS[o].w], [nx + S.w, qx], [nx, qx]]) if (Math.abs(a - b) < snap) nx += b - a;
            for (const [a, b] of [[ny, qy], [ny, qy + SCREENS[o].h], [ny + S.h, qy]]) if (Math.abs(a - b) < snap) ny += b - a;
          }
          L[k] = [Math.round(nx), Math.round(ny)];
          d.style.left = ox + L[k][0] * s + "px"; d.style.top = oy + L[k][1] * s + "px";
        };
        d.onpointerup = () => { d.onpointermove = null; emit(); };
      };
      el.append(d);
    }
  }
  onChange(draw); new ResizeObserver(draw).observe(el); draw();
}
// Screens not in the current profile, connected or remembered.
function unusedScreens() { return Object.keys(SCREENS).filter((k) => !P().layout[k]); }
function selectEl(list, val, on) {
  const s = document.createElement("select");
  list.forEach((t, i) => { const o = new Option(t, i); o.selected = i === val; s.append(o); });
  s.onchange = () => on(+s.value); return s;
}

(() => {
  const STEPS = ["Downloading", "Transcribing", "Finding the best moments", "Rendering clips"];
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;  // textContent only: titles come from outside
    return n;
  };

  if (!$("add")) return;  // the results page loads this file too

  // --- add form -------------------------------------------------------------------
  const form = $("add"), file = $("file"), drop = $("drop"), cap = $("captions_on");
  const syncCaps = () => { $("style_wrap").hidden = !cap.checked; };
  cap.addEventListener("change", syncCaps);
  syncCaps();
  const showName = () => { $("fname").textContent = file.files.length ? file.files[0].name : ""; };
  file.addEventListener("change", showName);
  ["dragover", "dragenter"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (ev) => {
    ev.preventDefault();
    if (ev.dataTransfer.files.length) { file.files = ev.dataTransfer.files; showName(); }
  });
  form.addEventListener("submit", () => {
    // a chosen file wins over the link box
    form.action = file.files.length ? "/jobs/upload" : "/jobs";
  });

  // --- queue ----------------------------------------------------------------------
  const post = (url) => fetch(url, { method: "POST" }).then(poll);
  const button = (cls, label, url) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", () => post(url));
    return b;
  };
  const fmtDur = (s) => { const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; };
  const fmtEta = (s) => (s < 90 ? "less than a minute left" : `about ${Math.round(s / 60)} min left`);
  const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "");

  function card(j) {
    const row = el("div", "job");
    if (j.has_thumb) { const i = el("img", "thumb"); i.src = `/media/thumbs/${j.id}.jpg`; i.alt = ""; row.append(i); }
    else row.append(el("div", "thumb"));
    const t = el("div", "t");
    t.append(el("b", null, j.duration ? `${j.title} — ${fmtDur(j.duration)}` : j.title));
    row.append(t);
    return [row, t];
  }

  function activeCard(j) {
    const [row, t] = card(j);
    const steps = el("div", "steps");
    for (let n = 1; n <= 4; n++) {
      const i = el("i", j.status === "running" ? (n < j.step ? "done" : n === j.step ? "now" : "") : "");
      if (i.className === "now") i.style.setProperty("--p", `${Math.round(j.step_progress * 100)}%`);
      steps.append(i);
    }
    t.append(steps);
    let text;
    if (j.cancel_requested) text = "Canceling…";
    else if (j.status === "queued") text = "Waiting · starts after the one above";
    else text = `Step ${j.step} of 4 · ${STEPS[j.step - 1] || "Starting"}${j.eta_seconds != null ? " · " + fmtEta(j.eta_seconds) : ""}`;
    t.append(el("div", "st", text));
    if (!j.cancel_requested) row.append(button("ghost", "Cancel", `/jobs/${j.id}/cancel`));
    return row;
  }

  function finishedCard(j) {
    const [row, t] = card(j);
    const when = fmtDate(j.finished_at || j.created_at);
    if (j.status === "done") {
      t.append(el("div", "st", `${when} · ${j.clip_count} clips`));
      const a = el("a", "pill ok", "Open →");
      a.href = `/jobs/${j.id}`;
      row.append(a);
    } else {
      const why = j.status === "canceled" ? "Canceled" : j.error || "Failed";
      t.append(el("div", "st", `${when} · ${why}`));
      row.append(button("pill err", "Retry", `/jobs/${j.id}/retry`));
    }
    return row;
  }

  const fill = (id, jobs, make, empty) => {
    const box = $(id);
    box.replaceChildren(...(jobs.length ? jobs.map(make) : [el("div", "small left", empty)]));
  };

  let timer = null;
  async function poll() {
    clearTimeout(timer);
    try {
      const r = await fetch("/api/jobs");
      if (r.status === 401) { location.href = "/login"; return; }
      const d = await r.json();
      fill("queue", d.active, activeCard, "Nothing running.");
      fill("finished", d.finished, finishedCard, "Nothing yet.");
      if (d.active.length) timer = setTimeout(poll, 2000);  // idle page makes no requests
    } catch (e) {
      timer = setTimeout(poll, 5000);
    }
  }
  poll();
})();

// --- results page ----------------------------------------------------------------
(() => {
  const root = document.getElementById("results");
  if (!root) return;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;  // textContent only: titles and words come from outside
    return n;
  };
  const btn = (cls, label, fn) => { const b = el("button", cls, label); b.type = "button"; b.addEventListener("click", fn); return b; };
  const duration = parseFloat(root.dataset.duration) || 0;
  const STYLES = JSON.parse(root.dataset.styles);
  const MIN = 3;
  const mmss = (t) => { const m = Math.floor(t / 60), s = t - m * 60; return `${m}:${s < 10 ? "0" : ""}${s.toFixed(1)}`; };
  const len = (t) => `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, "0")}`;

  async function send(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Something went wrong.");
    return d;
  }

  // --- re-render status: poll /api/jobs while any clip is busy ---------------------
  const busy = new Set();
  let timer = null;
  const showBusy = (card, on) => { card.querySelector(".rp-busy").hidden = !on; };
  async function poll() {
    clearTimeout(timer);
    try {
      const r = await fetch("/api/jobs");
      if (r.status === 401) { location.href = "/login"; return; }
      const active = new Set((await r.json()).rerendering);
      let finished = false;
      root.querySelectorAll(".rp-card").forEach((card) => {
        const id = Number(card.dataset.id);
        if (active.has(id)) { busy.add(id); showBusy(card, true); }
        else if (busy.has(id)) finished = true;
      });
      if (finished) { location.reload(); return; }  // new version is on disk: reload the card and its video
      if (active.size) timer = setTimeout(poll, 2000);
    } catch (e) {
      timer = setTimeout(poll, 5000);
    }
  }

  const closePanel = (card) => { const p = card.querySelector(".rp-panel"); p.hidden = true; p.replaceChildren(); };
  const started = (card) => { busy.add(Number(card.dataset.id)); showBusy(card, true); closePanel(card); poll(); };

  // --- delete overlay -------------------------------------------------------------
  function confirmDelete(card) {
    const o = el("div", "rp-confirm");
    const sub = el("div", "sub", "This can't be undone.");
    o.append(el("div", null, "Delete this clip?"), sub);
    const row = el("div", "row");
    const del = btn("danger", "Delete", async () => {
      del.disabled = true;
      try { await send(`/clips/${card.dataset.id}/delete`); location.reload(); }
      catch (e) { sub.textContent = e.message; del.disabled = false; }
    });
    row.append(btn("", "Cancel", () => o.remove()), del);
    o.append(row);
    card.querySelector(".rp-video").append(o);
  }

  // --- trim panel -----------------------------------------------------------------
  function trimPanel(card) {
    const p = card.querySelector(".rp-panel");
    p.replaceChildren();
    const cs = parseFloat(card.dataset.start), ce = parseFloat(card.dataset.end);
    const lo = Math.max(0, cs - 20), hi = Math.min(duration, ce + 20);  // the strip shows the clip plus some context
    let a = cs, b = ce;
    p.append(el("h3", null, "✂ Edit (trim)"), el("p", "sub", "Drag the yellow handles to move the start and end, then Save. The clip re-renders in a few seconds."));
    const track = el("div", "rp-trim"), sel = el("div", "rp-sel"), ha = el("div", "rp-h"), hb = el("div", "rp-h");
    ha.setAttribute("aria-label", "Start"); hb.setAttribute("aria-label", "End");
    track.append(sel, ha, hb);
    const read = el("div", "rp-row"), r1 = el("span"), r2 = el("span"), r3 = el("span");
    read.append(r1, r2, r3);
    const err = el("div", "msg err");
    const pct = (t) => ((t - lo) / (hi - lo)) * 100;
    const draw = () => {
      sel.style.left = `${pct(a)}%`; sel.style.right = `${100 - pct(b)}%`;
      ha.style.left = `${pct(a)}%`; hb.style.left = `${pct(b)}%`;
      r1.textContent = `Start ${mmss(a)}`; r2.textContent = `Length ${len(b - a)}`; r3.textContent = `End ${mmss(b)}`;
    };
    const drag = (h, which) => h.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      h.setPointerCapture(ev.pointerId);
      const move = (e) => {
        const box = track.getBoundingClientRect();
        const t = Math.round((lo + Math.min(1, Math.max(0, (e.clientX - box.left) / box.width)) * (hi - lo)) * 10) / 10;
        if (which === "a") a = Math.max(lo, Math.min(t, b - MIN)); else b = Math.min(hi, Math.max(t, a + MIN));
        draw();
      };
      const up = () => { h.removeEventListener("pointermove", move); h.removeEventListener("pointerup", up); };
      h.addEventListener("pointermove", move); h.addEventListener("pointerup", up);
    });
    drag(ha, "a"); drag(hb, "b");
    draw();
    const save = btn("pri", "Save & re-render", async () => {
      save.disabled = true;
      try { await send(`/clips/${card.dataset.id}/trim`, { start: a, end: b }); started(card); }
      catch (e) { err.textContent = e.message; save.disabled = false; }
    });
    const row = el("div", "rp-btns");
    row.append(btn("", "Cancel", () => closePanel(card)), save);
    p.append(track, read, err, row);
    p.hidden = false;
  }

  // --- caption panel --------------------------------------------------------------
  async function captionPanel(card) {
    const p = card.querySelector(".rp-panel");
    p.replaceChildren(el("p", "sub", "Loading…"));
    p.hidden = false;
    let d;
    try {
      const r = await fetch(`/api/clips/${card.dataset.id}/words`);
      if (!r.ok) throw new Error(r.status === 404 ? "No transcript for this video." : "Couldn't load the words.");
      d = await r.json();
    } catch (e) { p.replaceChildren(el("p", "msg err", e.message)); return; }

    let on = d.captions_on, style = d.caption_style;
    const text = d.words.map((w) => w.text);
    p.replaceChildren(el("h3", null, "Aa Caption"), el("p", "sub", "Turn captions on, fix misheard words, or pick a style."));
    const tog = el("label", "check");
    const box = el("input"); box.type = "checkbox"; box.checked = on;
    tog.append(box, " Captions on");
    const words = el("div", "rp-words"), styles = el("div", "rp-styles"), err = el("div", "msg err");
    const sync = () => { words.hidden = styles.hidden = !on; };
    box.addEventListener("change", () => { on = box.checked; sync(); });

    const drawWords = () => {
      words.replaceChildren(...d.words.map((w, i) => {
        const t = text[i];
        const chip = el("span", t !== w.orig ? "chip edited" : "chip", t || `(${w.orig} removed)`);
        if (!t) chip.classList.add("gone");
        chip.tabIndex = 0; chip.setAttribute("role", "button"); chip.title = "Click to edit; clear it to remove the word";
        const edit = () => {
          const inp = el("input", "chip-in"); inp.value = t; inp.maxLength = 40; inp.size = Math.max(4, t.length + 1);
          let done = false;
          const commit = () => { if (done) return; done = true; text[i] = inp.value.split(/\s+/).filter(Boolean).join(" "); drawWords(); };
          inp.addEventListener("blur", commit);
          inp.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") { done = true; drawWords(); } });
          chip.replaceWith(inp); inp.focus(); inp.select();
        };
        chip.addEventListener("click", edit);
        chip.addEventListener("keydown", (e) => { if (e.key === "Enter") edit(); });
        return chip;
      }));
    };
    drawWords();

    const drawStyles = () => {
      styles.replaceChildren(...Object.entries(STYLES).map(([k, label]) => {
        const s = btn(`style ${k}${k === style ? " on" : ""}`, label.toUpperCase(), () => { style = k; drawStyles(); });
        s.setAttribute("aria-pressed", String(k === style));
        return s;
      }));
    };
    drawStyles();
    sync();

    const save = btn("pri", "Save & re-render", async () => {
      save.disabled = true;
      const corrections = {};
      text.forEach((t, i) => { if (t !== d.words[i].orig) corrections[i] = t; });  // the FULL set; the server replaces
      try { await send(`/clips/${card.dataset.id}/captions`, { captions_on: on, caption_style: style, corrections }); started(card); }
      catch (e) { err.textContent = e.message; save.disabled = false; }
    });
    const row = el("div", "rp-btns");
    row.append(btn("", "Cancel", () => closePanel(card)), save);
    p.append(tog, words, styles, err, row);
  }

  root.querySelectorAll(".rp-card").forEach((card) => {
    const del = card.querySelector(".rp-del");
    if (del) del.addEventListener("click", () => confirmDelete(card));
    card.querySelector(".edit").addEventListener("click", () => trimPanel(card));
    card.querySelector(".cap").addEventListener("click", () => captionPanel(card));
  });
  if (root.querySelector("[data-busy]")) poll();
})();

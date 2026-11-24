(() => {
  const STEPS = ["Downloading", "Transcribing", "Finding the best moments", "Rendering clips"];
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;  // textContent only: titles come from outside
    return n;
  };

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

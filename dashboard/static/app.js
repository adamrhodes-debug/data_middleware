/* Integrations dashboard */

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, attrs = {}, kids = []) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  (Array.isArray(kids) ? kids : [kids]).forEach(c =>
    n.append(c instanceof Node ? c : document.createTextNode(c)));
  return n;
};

const num = n => (n === null || n === undefined) ? "—" : Number(n).toLocaleString();
const ago = ts => {
  if (!ts) return "never";
  const s = (Date.now() - new Date(ts)) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} hr ago`;
  return `${Math.round(s / 86400)} days ago`;
};

const get = async path => {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
};
const post = async (path, body) => {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
};

function panel(title, bodyNode, opts = {}) {
  const b = el("div", { class: "panel-body" + (opts.tight ? " tight" : "") },
                [bodyNode]);
  return el("div", { class: "panel" }, [el("h2", {}, title), b]);
}

function table(cols, rows, render) {
  if (!rows || !rows.length) return el("div", { class: "empty" }, "Nothing here yet.");
  const thead = el("thead", {}, [el("tr", {}, cols.map(c =>
    el("th", { class: c.num ? "num" : "" }, c.label)))]);
  const tbody = el("tbody", {}, rows.map(r => render(r)));
  return el("div", { class: "scroll" }, [el("table", {}, [thead, tbody])]);
}

function stat(value, label, tone) {
  return el("div", { class: "stat" }, [
    el("div", { class: "stat-value " + (tone || "") }, num(value)),
    el("div", { class: "label" }, label),
  ]);
}

/* ── Pipeline strip ─────────────────────────────────────────── */

let JOBS = [];

async function drawPipeline() {
  const data = await get("/api/overview");
  JOBS = data.jobs;
  const strip = $("#pipeline");
  strip.textContent = "";

  data.stages.forEach(s => {
    const dotClass = s.rows === null ? "off" : (s.warn ? "warn" : "");
    strip.append(el("div", { class: "stage" }, [
      el("div", { class: "stage-name" }, [
        el("span", { class: "dot " + dotClass }), s.label,
      ]),
      el("div", { class: "pipe-count" }, num(s.rows)),
      el("div", { class: "pipe-detail" + (s.warn ? " warn" : "") },
         s.detail + (s.last ? ` · ${ago(s.last)}` : "")),
    ]));
  });
}

/* ── Status tab ─────────────────────────────────────────────── */

async function renderStatus() {
  const host = $("#tab-status");
  host.textContent = "";
  const grid = el("div", { class: "grid" });
  host.append(grid);

  const revel = await get("/api/source/revel");
  if (revel.exists) {
    const body = el("div");
    body.append(table(
      [{ label: "Brand" }, { label: "Backfill" },
       { label: "Position", num: true }, { label: "Last run" }],
      revel.state,
      r => el("tr", {}, [
        el("td", {}, r.brand),
        el("td", {}, [el("span", {
          class: "pill " + (r.backfill_complete ? "ok" : "conflict"),
        }, r.backfill_complete ? "complete" : "in progress")]),
        el("td", { class: "num" }, num(r.backfill_offset)),
        el("td", {}, ago(r.last_run_at)),
      ])));
    grid.append(panel("Revel sync", body, { tight: true }));

    grid.append(panel("Revel by brand", table(
      [{ label: "Brand" }, { label: "Total", num: true },
       { label: "Usable email", num: true }, { label: "Opted in", num: true }],
      revel.by_brand,
      r => el("tr", {}, [
        el("td", {}, r.brand),
        el("td", { class: "num" }, num(r.total)),
        el("td", { class: "num" }, num(r.usable)),
        el("td", { class: "num" }, num(r.opted_in)),
      ])), { tight: true }));
  }

  const wifi = await get("/api/source/wifi");
  if (wifi.exists) {
    grid.append(panel("Wi-fi portal by venue", table(
      [{ label: "Market" }, { label: "Guests", num: true }],
      wifi.by_market,
      r => el("tr", {}, [
        el("td", {}, el("code", { class: "mono" }, r.market)),
        el("td", { class: "num" }, num(r.n)),
      ])), { tight: true }));
  }

  const runs = await get("/api/runs");
  grid.append(panel("Recent runs", table(
    [{ label: "Job" }, { label: "Started" }, { label: "Result" }],
    runs.rows,
    r => el("tr", {}, [
      el("td", {}, r.job),
      el("td", {}, ago(r.started_at)),
      el("td", {}, [el("span", { class: "pill " + r.status }, r.status)]),
    ])), { tight: true }));
}

/* ── Data quality ───────────────────────────────────────────── */

async function renderQuality() {
  const host = $("#tab-quality");
  host.textContent = "";
  const q = await get("/api/quality");
  if (!q.exists) {
    host.append(el("div", { class: "panel" },
      el("div", { class: "empty" }, "Build the master table first.")));
    return;
  }

  const t = q.totals;
  const stats = el("div", { class: "stats" }, [
    stat(t.people, "People"),
    stat(t.has_first, "With first name"),
    stat(t.has_last, "With last name"),
    stat(t.has_birthday, "With birthday"),
    stat(t.has_nationality, "With nationality",
         t.has_nationality === 0 ? "attention" : ""),
    stat(t.multi_source, "In 2+ sources"),
  ]);
  host.append(panel("Master table", stats, { tight: true }));

  const grid = el("div", { class: "grid" });
  grid.style.marginTop = "20px";
  host.append(grid);

  grid.append(panel("Tags", table(
    [{ label: "Tag" }, { label: "People", num: true }],
    q.tags,
    r => el("tr", {}, [
      el("td", {}, el("span", { class: "tag" }, r.tag)),
      el("td", { class: "num" }, num(r.n)),
    ])), { tight: true }));

  grid.append(panel("Source overlap", table(
    [{ label: "Appears in" }, { label: "People", num: true }],
    q.sources,
    r => el("tr", {}, [
      el("td", {}, r.combo),
      el("td", { class: "num" }, num(r.n)),
    ])), { tight: true }));

  const nb = el("div");
  nb.append(el("div", { class: "stats" }, [
    stat(q.no_brand, "No brand tag", q.no_brand > 0 ? "attention" : "good"),
  ]));
  if (q.no_brand > 0) {
    nb.append(table(
      [{ label: "Email" }, { label: "Tags" }],
      q.no_brand_sample,
      r => el("tr", {}, [
        el("td", {}, r.email),
        el("td", {}, (r.tags || []).map(x => el("span", { class: "tag" }, x))),
      ])));
    nb.append(el("div", { class: "note" },
      "These customers can't be routed to a Como account. Check their source's tag settings in Registry."));
  }
  grid.append(panel("Unroutable", nb, { tight: true }));

  const dup = el("div");
  dup.append(el("div", { class: "stats" }, [
    stat(q.duplicates, "Held back as duplicates",
         q.duplicates > 0 ? "attention" : "good"),
  ]));
  if (q.duplicates > 0) {
    dup.append(table(
      [{ label: "Address" }, { label: "Same inbox as" }],
      q.duplicate_sample,
      r => el("tr", {}, [
        el("td", {}, r.email),
        el("td", {}, r.duplicate_of),
      ])));
    dup.append(el("div", { class: "note" },
      "These reach the same inbox as another record (Gmail ignores dots and anything after a +). Only one of each set is pushed to Como."));
  }
  grid.append(panel("Duplicates", dup, { tight: true }));
}

/* ── Consent ────────────────────────────────────────────────── */

async function renderConsent() {
  const host = $("#tab-consent");
  host.textContent = "";
  const c = await get("/api/consent");
  if (!c.exists) {
    host.append(el("div", { class: "panel" },
      el("div", { class: "empty" }, "Build the master table first.")));
    return;
  }

  const o = c.overall;
  host.append(panel("Overall", el("div", { class: "stats" }, [
    stat(o.consented, "Can email", "good"),
    stat(o.opted_out, "Opted out", "bad"),
    stat(o.unknown, "Unknown", o.unknown > 0 ? "attention" : ""),
  ]), { tight: true }));

  const grid = el("div", { class: "grid" });
  grid.style.marginTop = "20px";
  host.append(grid);

  const cols = [{ label: "" }, { label: "Can email", num: true },
                { label: "Opted out", num: true }, { label: "Unknown", num: true }];

  grid.append(panel("By source", table(cols, c.by_source, r => el("tr", {}, [
    el("td", {}, r.combo),
    el("td", { class: "num" }, num(r.consented)),
    el("td", { class: "num" }, num(r.opted_out)),
    el("td", { class: "num" }, num(r.unknown)),
  ])), { tight: true }));

  grid.append(panel("By brand", table(cols, c.by_brand, r => el("tr", {}, [
    el("td", {}, r.brand),
    el("td", { class: "num" }, num(r.consented)),
    el("td", { class: "num" }, num(r.opted_out)),
    el("td", { class: "num" }, num(r.unknown)),
  ])), { tight: true }));

  if (o.unknown > 0) {
    host.append(el("div", { class: "panel", style: "margin-top:20px" },
      el("div", { class: "note" },
        "Unknown means no source recorded a consent decision. The wi-fi portal asks for consent but doesn't save the answer — fixing that in index.html is what moves these into a known state.")));
  }
}

/* ── Push ───────────────────────────────────────────────────── */

async function renderPush() {
  const host = $("#tab-push");
  host.textContent = "";
  const p = await get("/api/push");
  if (!p.exists) {
    host.append(el("div", { class: "panel" },
      el("div", { class: "empty" }, "Build the master table first.")));
    return;
  }

  host.append(panel("By brand", table(
    [{ label: "Brand" }, { label: "Key" }, { label: "Tagged", num: true },
     { label: "Pushed", num: true }, { label: "Pending", num: true },
     { label: "Failed", num: true }, { label: "Conflicts", num: true },
     { label: "Progress" }],
    p.brands,
    r => {
      const pct = r.tagged ? Math.round(100 * r.ok / r.tagged) : 0;
      return el("tr", {}, [
        el("td", {}, r.brand),
        el("td", {}, [el("span", { class: "pill " + (r.configured ? "ok" : "conflict") },
                          r.configured ? "set up" : "no key")]),
        el("td", { class: "num" }, num(r.tagged)),
        el("td", { class: "num" }, num(r.ok)),
        el("td", { class: "num" }, num(r.pending)),
        el("td", { class: "num" }, num(r.failed)),
        el("td", { class: "num" }, num(r.conflict)),
        el("td", {}, el("div", { class: "bar" },
          el("span", { style: `width:${pct}%` }))),
      ]);
    }), { tight: true }));

  const exp = el("div", { class: "row" }, [
    el("span", {}, "Download master table in Como's CSV format:"),
    el("a", { href: "/export.csv" },
       el("button", { class: "action quiet" }, "All brands")),
    ...p.brands.map(b => el("a", { href: `/export.csv?brand=${b.brand}` },
       el("button", { class: "action quiet" }, b.brand))),
  ]);
  // ── Pre-flight: prove nothing dodgy is queued ──────────────────
  const pf = await get("/api/preflight");
  const pfBody = el("div");

  pfBody.append(el("div", { class: "stats" }, [
    stat(pf.queued, "Queued to push"),
    el("div", { class: "stat" }, [
      el("div", {
        class: "stat-value " + (pf.all_clear ? "good" : "attention"),
        style: "font-size:18px",
      }, pf.all_clear ? "All clear" : "Needs a look"),
      el("div", { class: "label" }, "Safety checks"),
    ]),
  ]));

  pfBody.append(table(
    [{ label: "Check" }, { label: "Result" }, { label: "Affected", num: true }],
    pf.checks,
    c => el("tr", {}, [
      el("td", {}, [
        el("div", {}, c.name),
        el("div", { class: "label", style: "text-transform:none;letter-spacing:0" },
           c.detail),
      ]),
      el("td", {}, [el("span", { class: "pill " + (c.pass ? "ok" : "conflict") },
                        c.pass ? "pass" : "check")]),
      el("td", { class: "num" }, num(c.count)),
    ])));

  const failed = pf.checks.filter(c => !c.pass);
  if (failed.length) {
    failed.forEach(c => {
      pfBody.append(el("div", { class: "note" },
        c.name + " — examples: " +
        c.sample.map(x => x.email || x.emails?.join(" / ") || "").join(", ")));
    });
  }

  host.append(el("div", { style: "margin-top:20px" },
    panel("Before you push", pfBody, { tight: true })));

  // ── Main push, paced ───────────────────────────────────────────
  const brandSel = el("select", {}, [
    el("option", { value: "ALL" }, "All configured brands"),
    ...p.brands.filter(b => b.configured)
              .map(b => el("option", { value: b.brand }, b.brand)),
  ]);
  const rateIn = el("input", { type: "text", value: "300", style: "width:70px" });
  const batchIn = el("input", { type: "text", placeholder: "off",
                                style: "width:70px" });
  const pauseIn = el("input", { type: "text", placeholder: "off",
                                style: "width:70px" });
  const limitIn = el("input", { type: "text", placeholder: "all",
                                style: "width:70px" });
  const estimate = el("div", { class: "note", style: "border:none;padding:8px 0" },
                      "…");
  const pushOut = el("pre", { class: "output" },
    "Nothing running. Preview first, then start.");
  const startBtn = el("button", { class: "action" }, "Start push");
  const previewBtn = el("button", { class: "action quiet" }, "Preview");
  let pushPoll = null;

  const refreshEstimate = async () => {
    const q = new URLSearchParams({
      brand: brandSel.value,
      rate: rateIn.value || 0,
      batch_size: batchIn.value || 0,
      batch_pause: pauseIn.value || 0,
    });
    try {
      const e = await get("/api/pushplan?" + q);
      estimate.textContent =
        `${num(e.queued)} queued · about ${num(e.api_calls)} API calls · ` +
        (e.batches ? `${num(e.batches)} batches · ` : "") +
        `roughly ${e.human}`;
      estimate.style.color = e.over_limit ? "var(--fail)" : "";
      if (e.over_limit) {
        estimate.textContent += "  ⚠ above Como's 500/min limit";
      }
    } catch (err) {
      estimate.textContent = "Couldn't work out an estimate.";
    }
  };

  [brandSel, rateIn, batchIn, pauseIn].forEach(i => {
    i.addEventListener("change", refreshEstimate);
    i.addEventListener("keyup", refreshEstimate);
  });
  refreshEstimate();

  const startPush = async (dryRun) => {
    if (!dryRun) {
      const e = estimate.textContent;
      if (!confirm(`This sends real data to Como.\n\n${e}\n\nGo ahead?`)) return;
    }
    startBtn.disabled = previewBtn.disabled = true;
    pushOut.textContent = "Starting…";
    const { run_id, command } = await post("/api/mainpush", {
      brand: brandSel.value,
      rate: rateIn.value,
      batch_size: batchIn.value,
      batch_pause: pauseIn.value,
      // Preview samples rather than enumerating everything - 20 records
      // answers "does the payload look right" as well as 4,000 would
      limit: dryRun ? (limitIn.value || 20) : (limitIn.value || null),
      dry_run: dryRun,
    });
    pushOut.textContent = "$ " + command + "\n";
    if (pushPoll) clearInterval(pushPoll);
    pushPoll = setInterval(async () => {
      const r = await get(`/api/run/${run_id}/output`);
      pushOut.textContent = "$ " + command + "\n\n" + (r.output || "…");
      pushOut.scrollTop = pushOut.scrollHeight;
      if (r.status !== "running") {
        clearInterval(pushPoll);
        startBtn.disabled = previewBtn.disabled = false;
        drawPipeline();
        refreshEstimate();
      }
    }, 2000);
  };

  startBtn.addEventListener("click", () => startPush(false));
  previewBtn.addEventListener("click", () => startPush(true));

  host.append(el("div", { style: "margin-top:20px" },
    panel("Push to Como", el("div", {}, [
      el("div", { class: "row", style: "margin-bottom:10px" }, [
        el("span", { class: "label" }, "Brand"), brandSel,
        el("span", { class: "label" }, "Calls/min"), rateIn,
        el("span", { class: "label" }, "Cap at"), limitIn,
      ]),
      el("div", { class: "row", style: "margin-bottom:10px" }, [
        el("span", { class: "label" }, "Optional pacing — batch of"), batchIn,
        el("span", { class: "label" }, "then wait (sec)"), pauseIn,
      ]),
      estimate,
      el("div", { class: "row", style: "margin-bottom:12px" },
         [previewBtn, startBtn]),
      pushOut,
      el("div", { class: "note" },
        "Como allows 500 calls per minute. 300 leaves headroom without " +
        "dragging the run out. The batch fields are only useful if you " +
        "want to spread a run over hours — at 300/min you shouldn't need " +
        "them. Preview samples 20 records unless you set a cap. The run " +
        "keeps going if you close this tab."),
    ]))));

  // ── Test push: one person ──────────────────────────────────────
  const testEmail = el("input", { type: "text",
    placeholder: "someone@example.com", style: "min-width:260px" });
  const testOut = el("pre", { class: "output" },
    "Pushes one person and shows what Como stores afterwards.");
  let testPoll = null;

  const runTest = async (dryRun) => {
    const email = testEmail.value.trim();
    if (!email) return;
    testOut.textContent = "Working…";
    const { run_id, error } = await post("/api/testpush", { email, dry_run: dryRun });
    if (error) { testOut.textContent = error; return; }
    if (testPoll) clearInterval(testPoll);
    testPoll = setInterval(async () => {
      const r = await get(`/api/run/${run_id}/output`);
      testOut.textContent = r.output || "…";
      testOut.scrollTop = testOut.scrollHeight;
      if (r.status !== "running") clearInterval(testPoll);
    }, 1000);
  };

  host.append(el("div", { style: "margin-top:20px" },
    panel("Test with one person", el("div", {}, [
      el("div", { class: "row", style: "margin-bottom:12px" }, [
        testEmail,
        el("button", { class: "action quiet",
                       onclick: () => runTest(true) }, "Preview only"),
        el("button", { class: "action",
                       onclick: () => runTest(false) }, "Push this one"),
      ]),
      testOut,
    ]))));

  const wrap = el("div", { style: "margin-top:20px" }, [panel("Export", exp)]);
  host.append(wrap);
}

/* ── Conflicts ──────────────────────────────────────────────── */

async function renderConflicts() {
  const host = $("#tab-conflicts");
  host.textContent = "";
  const c = await get("/api/conflicts");

  host.append(panel("Needs a decision", table(
    [{ label: "Email" }, { label: "Brand" }, { label: "What Como said" },
     { label: "When" }, { label: "" }],
    c.rows,
    r => el("tr", {}, [
      el("td", {}, r.email),
      el("td", {}, r.brand),
      el("td", {}, el("code", { class: "mono" }, (r.detail || "").slice(0, 90))),
      el("td", {}, ago(r.last_pushed_at)),
      el("td", {}, el("button", {
        class: "action quiet",
        onclick: async ev => {
          ev.target.disabled = true;
          await post("/api/conflicts/resolve",
                     { email: r.email, brand: r.brand });
          renderConflicts();
        },
      }, "Clear")),
    ])), { tight: true }));

  host.append(el("div", { class: "panel", style: "margin-top:20px" },
    el("div", { class: "note" },
      "A conflict means the email already belongs to a different Como membership. Como's own fix sends a verification code to the customer, so it isn't done automatically. Clearing a row makes it eligible for another push attempt.")));
}

/* ── Lookup ─────────────────────────────────────────────────── */

async function renderLookup() {
  const host = $("#tab-lookup");
  host.textContent = "";

  const input = el("input", { type: "search", placeholder: "customer@example.com",
                              style: "min-width:280px" });
  const results = el("div", { style: "margin-top:20px" });

  const search = async () => {
    const email = input.value.trim().toLowerCase();
    if (!email) return;
    results.textContent = "";
    const d = await get("/api/customer?email=" + encodeURIComponent(email));

    if (!d.master) {
      results.append(el("div", { class: "panel" },
        el("div", { class: "empty" }, "Not in the master table.")));
    } else {
      const m = d.master;
      const rows = [
        ["First name", m.first_name || "—"],
        ["Last name", m.last_name || "—"],
        ["Birthday", m.birthday || "—"],
        ["Nationality", m.nationality || "—"],
        ["Can email", m.allow_email === null ? "unknown" : String(m.allow_email)],
        ["Sources", (m.sources || []).join(", ")],
        ["Awaiting push", String(m.needs_push)],
      ];
      const body = el("table", {}, el("tbody", {}, rows.map(([k, v]) =>
        el("tr", {}, [el("td", { style: "width:150px" },
                         el("span", { class: "label" }, k)),
                      el("td", {}, String(v))]))));
      results.append(panel("Master record", body, { tight: true }));

      const tagBox = el("div", { class: "panel-body" },
        (m.tags || []).map(t => el("span", { class: "tag" }, t)));
      results.append(el("div", { style: "margin-top:20px" },
        el("div", { class: "panel" }, [el("h2", {}, "Tags"), tagBox])));
    }

    if (d.push && d.push.length) {
      results.append(el("div", { style: "margin-top:20px" }, panel("Push history", table(
        [{ label: "Brand" }, { label: "Result" }, { label: "When" }, { label: "Detail" }],
        d.push,
        r => el("tr", {}, [
          el("td", {}, r.brand),
          el("td", {}, [el("span", { class: "pill " + r.status }, r.status)]),
          el("td", {}, ago(r.last_pushed_at)),
          el("td", {}, el("code", { class: "mono" }, (r.detail || "").slice(0, 60))),
        ])), { tight: true })));
    }

    // What would be sent
    try {
      const p = await get("/api/preview?email=" + encodeURIComponent(email));
      results.append(el("div", { style: "margin-top:20px" },
        panel("What would be sent to Como", el("div", {}, [
          el("div", { class: "label", style: "margin-bottom:8px" },
             "Goes to: " + (p.brands.length ? p.brands.join(", ") : "nowhere — no brand tag")),
          el("pre", { class: "output" }, JSON.stringify(p.request, null, 2)),
        ]))));
    } catch (e) { /* not in master, already reported */ }

    for (const [name, rows] of Object.entries(d.sources || {})) {
      if (!rows || !rows.length) continue;
      results.append(el("div", { style: "margin-top:20px" },
        panel("Raw: " + name,
          el("pre", { class: "output" }, JSON.stringify(rows, null, 2)))));
    }
  };

  input.addEventListener("keydown", e => { if (e.key === "Enter") search(); });

  host.append(panel("Find a customer", el("div", { class: "row" }, [
    input, el("button", { class: "action", onclick: search }, "Search"),
  ])));
  host.append(results);
}

/* ── Run jobs ───────────────────────────────────────────────── */

let polling = null;

async function renderRun() {
  const host = $("#tab-run");
  host.textContent = "";

  const out = el("pre", { class: "output" }, "Pick a job to run.");
  const status = el("span", { class: "label" }, "");

  const buttons = JOBS.map(j => el("button", {
    class: "action",
    onclick: async ev => {
      document.querySelectorAll("#tab-run button.action")
        .forEach(b => b.disabled = true);
      out.textContent = "Starting…";
      status.textContent = "running";
      const { run_id } = await post("/api/run/" + j.key);
      if (polling) clearInterval(polling);
      polling = setInterval(async () => {
        const r = await get(`/api/run/${run_id}/output`);
        out.textContent = r.output || "…";
        out.scrollTop = out.scrollHeight;
        status.textContent = r.status;
        if (r.status !== "running") {
          clearInterval(polling);
          document.querySelectorAll("#tab-run button.action")
            .forEach(b => b.disabled = false);
          drawPipeline();
        }
      }, 1200);
    },
  }, j.label));

  host.append(panel("Jobs", el("div", { class: "row" }, buttons)));
  host.append(el("div", { style: "margin-top:20px" },
    panel("Output", el("div", {}, [status, out]), { })));
}

/* ── Registry ───────────────────────────────────────────────── */

async function renderRegistry() {
  const host = $("#tab-registry");
  host.textContent = "";
  const r = await get("/api/registry");

  const editable = ["source_tag", "extra_tags_expr", "email_expr",
                    "first_name_expr", "last_name_expr", "nationality_expr",
                    "birthday_expr", "allow_email_expr", "where_extra",
                    "priority"];

  const srcRows = r.sources.map(s => {
    const cells = [el("td", {}, el("code", { class: "mono" }, s.source_table))];
    editable.forEach(f => {
      const input = el("input", { type: "text", value: s[f] === null ? "" : s[f],
                                  style: "width:100%" });
      input.addEventListener("change", async () => {
        await post("/api/registry/source",
          { source_table: s.source_table, field: f, value: input.value });
        input.style.borderColor = "var(--ok)";
        setTimeout(() => input.style.borderColor = "", 900);
      });
      cells.push(el("td", {}, input));
    });
    return el("tr", {}, cells);
  });

  const srcTable = el("div", { class: "scroll", style: "overflow-x:auto" }, [
    el("table", { style: "min-width:1100px" }, [
      el("thead", {}, el("tr", {}, [el("th", {}, "Table"),
        ...editable.map(f => el("th", {}, f.replace(/_expr$/, "")))])),
      el("tbody", {}, srcRows),
    ])]);

  host.append(panel("Sources", el("div", {}, [
    srcTable,
    el("div", { class: "note" },
      "Values are SQL evaluated against that table — usually a column name. Changes save as you leave each box. Run “Rebuild master table” afterwards."),
  ]), { tight: true }));

  const grid = el("div", { class: "grid" });
  grid.style.marginTop = "20px";
  host.append(grid);

  // Blocked domains
  const blockedInput = el("input", { type: "text", placeholder: "example.com" });
  const blockedList = el("div", { class: "panel-body" },
    r.blocked.map(b => el("span", { class: "tag" }, [
      b.domain, " ",
      el("button", {
        class: "action quiet", style: "padding:0 4px;border:none",
        onclick: async () => {
          await post("/api/registry/list",
            { list: "blocked", action: "remove", value: b.domain });
          renderRegistry();
        },
      }, "×"),
    ])));
  grid.append(el("div", { class: "panel" }, [
    el("h2", {}, "Blocked domains"),
    blockedList,
    el("div", { class: "panel-body", style: "border-top:1px solid var(--rule)" },
      el("div", { class: "row" }, [
        blockedInput,
        el("button", {
          class: "action",
          onclick: async () => {
            if (!blockedInput.value.trim()) return;
            await post("/api/registry/list",
              { list: "blocked", action: "add", value: blockedInput.value });
            renderRegistry();
          },
        }, "Block"),
      ])),
  ]));

  // Country map
  const slugInput = el("input", { type: "text", placeholder: "united-arab-emirates" });
  const tagInput = el("input", { type: "text", placeholder: "UAE", style: "width:90px" });
  const countryTable = table(
    [{ label: "In market string" }, { label: "Becomes tag" }, { label: "" }],
    r.countries,
    c => el("tr", {}, [
      el("td", {}, el("code", { class: "mono" }, c.slug)),
      el("td", {}, el("span", { class: "tag" }, c.tag)),
      el("td", {}, el("button", {
        class: "action quiet",
        onclick: async () => {
          await post("/api/registry/list",
            { list: "country", action: "remove", value: c.slug });
          renderRegistry();
        },
      }, "Remove")),
    ]));
  grid.append(el("div", { class: "panel" }, [
    el("h2", {}, "Countries"),
    countryTable,
    el("div", { class: "panel-body", style: "border-top:1px solid var(--rule)" },
      el("div", { class: "row" }, [
        slugInput, tagInput,
        el("button", {
          class: "action",
          onclick: async () => {
            if (!slugInput.value.trim() || !tagInput.value.trim()) return;
            await post("/api/registry/list", {
              list: "country", action: "add",
              value: slugInput.value, tag: tagInput.value,
            });
            renderRegistry();
          },
        }, "Add"),
      ])),
  ]));
}

/* ── Tabs ───────────────────────────────────────────────────── */

const RENDER = {
  status: renderStatus, quality: renderQuality, consent: renderConsent,
  push: renderPush, conflicts: renderConflicts, lookup: renderLookup,
  run: renderRun, registry: renderRegistry,
};

document.querySelectorAll("nav.tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav.tabs button")
      .forEach(b => b.setAttribute("aria-selected", String(b === btn)));
    document.querySelectorAll("main section")
      .forEach(s => s.classList.add("hidden"));
    const name = btn.dataset.tab;
    $("#tab-" + name).classList.remove("hidden");
    RENDER[name]().catch(e => {
      $("#tab-" + name).textContent = "";
      $("#tab-" + name).append(el("div", { class: "panel" },
        el("div", { class: "empty" }, "Couldn't load: " + e.message)));
    });
  });
});

function tick() {
  $("#clock").textContent = new Date().toLocaleString("en-GB", {
    dateStyle: "medium", timeStyle: "short",
  });
}

tick();
setInterval(tick, 30000);
drawPipeline();
renderStatus();
setInterval(drawPipeline, 60000);
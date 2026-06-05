"use strict";

const $ = (sel) => document.querySelector(sel);

let pollTimer = null;

async function loadCapabilities() {
  try {
    const caps = await fetch("/api/capabilities").then((r) => r.json());
    const box = $("#difficulties");
    box.innerHTML = "";
    const defaults = new Set(["Easy", "Medium", "Hard"]);
    caps.difficulties.forEach((name) => {
      const label = document.createElement("label");
      label.className = "chip" + (defaults.has(name) ? " active" : "");
      label.innerHTML =
        `<input type="checkbox" value="${name}" ${defaults.has(name) ? "checked" : ""}/>${name}`;
      label.querySelector("input").addEventListener("change", (e) => {
        label.classList.toggle("active", e.target.checked);
      });
      box.appendChild(label);
    });

    const asBox = $("#autostepper");
    if (!caps.autostepper) {
      asBox.checked = false;
      asBox.disabled = true;
      asBox.parentElement.append(" (not installed)");
    }
  } catch (e) {
    console.error(e);
  }
}

function selectedDifficulties() {
  return [...document.querySelectorAll("#difficulties input:checked")].map(
    (i) => i.value
  );
}

let lastFetchedUrl = "";

async function fetchMetadata(manual) {
  const url = $("#url").value.trim();
  const msg = $("#fetch-msg");
  if (!url) {
    if (manual) {
      msg.textContent = "Enter a YouTube URL first.";
      msg.className = "hint err";
    }
    return;
  }
  if (!manual && url === lastFetchedUrl) return;
  lastFetchedUrl = url;

  $("#fetch").disabled = true;
  msg.textContent = "Fetching title & artist\u2026";
  msg.className = "hint";
  try {
    const meta = await fetch(
      "/api/metadata?url=" + encodeURIComponent(url)
    ).then((r) => r.json());
    if (meta.error) throw new Error(meta.error);
    if (meta.title) $("#title").value = meta.title;
    if (meta.artist) $("#artist").value = meta.artist;
    if (meta.title || meta.artist) {
      msg.textContent = "Detected \u2014 edit if needed.";
      msg.className = "hint ok";
    } else {
      msg.textContent = "Couldn't auto-detect. Fill in manually.";
      msg.className = "hint err";
    }
  } catch (e) {
    msg.textContent = "Couldn't fetch info: " + e.message;
    msg.className = "hint err";
  } finally {
    $("#fetch").disabled = false;
  }
}

function setProgress(pct, msg) {
  $("#progress").classList.remove("hidden");
  $("#bar-fill").style.width = `${pct}%`;
  $("#progress-msg").textContent = msg || "";
}

// Lower-is-better metrics get the "good" highlight when they're the column min.
const LOWER = ["onset_align_ms", "lane_imbalance", "crossover_rate", "candle_rate"];
const HIGHER = ["onset_recall"];

function fmt(v) {
  if (v === null || v === undefined) return "\u2013";
  return typeof v === "number" ? v : v;
}
function renderResults(result) {
  $("#results").classList.remove("hidden");
  $("#song-info").innerHTML =
    `<strong>${escapeHtml(result.title)}</strong> &mdash; ` +
    `${escapeHtml(result.artist)} &middot; BPM ${result.bpm} &middot; ` +
    `${result.duration}s`;

  const host = $("#generators");
  host.innerHTML = "";

  const working = result.generators.filter(
    (g) => !g.error && g.charts && g.charts.length
  );

  host.appendChild(buildSummary(working));

  result.generators.forEach((gen) => {
    host.appendChild(buildGeneratorCard(gen));
  });

  host.appendChild(buildLegend());
}

// Average a numeric metric across a generator's charts.
function avg(charts, key) {
  const vals = charts.map((c) => c[key]).filter((v) => typeof v === "number");
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

function syncLabel(ms) {
  if (ms == null) return { text: "\u2013", cls: "" };
  if (ms <= 60) return { text: "Excellent", cls: "q-good" };
  if (ms <= 100) return { text: "Good", cls: "q-ok" };
  return { text: "Loose", cls: "q-warn" };
}

function comfortLabel(candlePerMin, crossover) {
  const score = (candlePerMin || 0) + (crossover || 0) * 100;
  if (score <= 1) return { text: "Very comfortable", cls: "q-good" };
  if (score <= 6) return { text: "Comfortable", cls: "q-ok" };
  return { text: "Some awkward steps", cls: "q-warn" };
}

// A plain-language comparison of the working generators.
function buildSummary(working) {
  const box = document.createElement("div");
  box.className = "summary";
  if (!working.length) {
    box.innerHTML = "<p class='muted'>No generator produced a chart.</p>";
    return box;
  }

  // Find the best (lowest) average sync.
  let best = null;
  working.forEach((g) => {
    const s = avg(g.charts, "onset_align_ms");
    if (s != null && (best == null || s < best.sync)) {
      best = { name: g.name, sync: s };
    }
  });

  let html = "<h3>In short</h3><ul class='plain'>";
  working.forEach((g) => {
    const sync = avg(g.charts, "onset_align_ms");
    const sl = syncLabel(sync);
    const cl = comfortLabel(
      avg(g.charts, "candle_rate"),
      avg(g.charts, "crossover_rate")
    );
    const star = best && g.name === best.name ? " \u2b50" : "";
    html +=
      `<li><strong>${escapeHtml(g.name)}</strong>${star}: ` +
      `${g.charts.length} difficult${g.charts.length === 1 ? "y" : "ies"}, ` +
      `timing <span class='${sl.cls}'>${sl.text.toLowerCase()}</span> ` +
      `(~${sync == null ? "?" : Math.round(sync)} ms), ` +
      `<span class='${cl.cls}'>${cl.text.toLowerCase()}</span>.</li>`;
  });
  html += "</ul>";
  if (best) {
    html +=
      `<p class='muted'>\u2b50 Best timing sync: <strong>` +
      `${escapeHtml(best.name)}</strong> (lower ms = more on the beat).</p>`;
  }
  box.innerHTML = html;
  return box;
}

function buildGeneratorCard(gen) {
  const div = document.createElement("div");
  div.className = "gen";
  const ok = !gen.error && gen.charts && gen.charts.length;

  if (!ok) {
    div.innerHTML =
      `<h3>${escapeHtml(gen.name)} ` +
      `<span class="badge err">couldn't generate</span></h3>` +
      `<p class="muted">${escapeHtml(gen.error || "not available")}</p>`;
    return div;
  }

  div.innerHTML =
    `<h3>${escapeHtml(gen.name)} ` +
    `<span class="badge">${gen.charts.length} charts</span></h3>`;
  div.appendChild(buildTable(gen.charts));

  if (gen.folder) {
    const rel = gen.folder
      .split(/output[\\/]+web[\\/]+/)
      .pop()
      .replace(/\\/g, "/");
    const a = document.createElement("a");
    a.className = "dl";
    a.href = `/download_zip?dir=${encodeURIComponent(rel)}`;
    a.textContent = "\u2b07 Download playable folder (.zip)";
    div.appendChild(a);
  }
  return div;
}

function buildTable(charts) {
  const cols = [
    ["difficulty", "Difficulty", null],
    ["nps", "Steps/sec", null],
    ["onset_align_ms", "Sync (ms)", "low"],
    ["onset_recall", "Beats hit", "high"],
    ["lane_imbalance", "Lane balance", "low"],
    ["candle_rate", "Awkward/min", "low"],
  ];
  // Best value per column for highlighting.
  const bestOf = {};
  cols.forEach(([k, , dir]) => {
    if (!dir) return;
    const vals = charts.map((c) => c[k]).filter((v) => typeof v === "number");
    if (!vals.length) return;
    bestOf[k] = dir === "low" ? Math.min(...vals) : Math.max(...vals);
  });

  const table = document.createElement("table");
  table.innerHTML =
    "<thead><tr>" +
    cols.map(([, h]) => `<th>${h}</th>`).join("") +
    "</tr></thead>";
  const tbody = document.createElement("tbody");
  charts.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = cols
      .map(([k, , dir]) => {
        let v = c[k];
        if (k === "onset_recall" && typeof v === "number") {
          v = Math.round(v * 100) + "%";
        } else if (k === "onset_align_ms" && typeof v === "number") {
          v = Math.round(v);
        }
        const good =
          dir && typeof c[k] === "number" && c[k] === bestOf[k]
            ? " class='good'"
            : "";
        return `<td${good}>${fmt(v)}</td>`;
      })
      .join("");
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

function buildLegend() {
  const d = document.createElement("div");
  d.className = "legend muted";
  d.innerHTML =
    "<strong>What the columns mean:</strong> " +
    "<em>Steps/sec</em> = how busy the chart is. " +
    "<em>Sync</em> = how close steps land to the music (lower is better). " +
    "<em>Beats hit</em> = share of detected beats that got a step (higher is better). " +
    "<em>Lane balance</em> = how evenly the 4 arrows are used (lower is better). " +
    "<em>Awkward/min</em> = uncomfortable moves per minute (lower is better). " +
    "Green = best value in that column.";
  return d;
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function poll(jobId) {
  const job = await fetch(`/api/status?job=${jobId}`).then((r) => r.json());
  setProgress(job.pct || 0, job.message || "");

  if (job.status === "done") {
    clearInterval(pollTimer);
    $("#progress").classList.add("hidden");
    $("#go").disabled = false;
    renderResults(job.result);
  } else if (job.status === "error") {
    clearInterval(pollTimer);
    setProgress(0, "Error: " + job.message);
    $("#go").disabled = false;
  }
}

$("#gen-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#results").classList.add("hidden");
  $("#go").disabled = true;
  setProgress(2, "Submitting\u2026");

  const body = {
    url: $("#url").value.trim(),
    title: $("#title").value.trim(),
    artist: $("#artist").value.trim(),
    difficulties: selectedDifficulties(),
    autostepper: $("#autostepper").checked,
  };

  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json());

    if (!res.job) throw new Error(res.error || "no job id");
    pollTimer = setInterval(() => poll(res.job), 1200);
  } catch (err) {
    setProgress(0, "Error: " + err.message);
    $("#go").disabled = false;
  }
});

$("#fetch").addEventListener("click", () => fetchMetadata(true));
$("#url").addEventListener("blur", () => fetchMetadata(false));

loadCapabilities();

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
    `<strong>${result.title}</strong> &mdash; ${result.artist} &middot; ` +
    `BPM ${result.bpm} &middot; ${result.duration}s`;

  const host = $("#generators");
  host.innerHTML = "";

  result.generators.forEach((gen) => {
    const div = document.createElement("div");
    div.className = "gen";
    const ok = !gen.error && gen.charts && gen.charts.length;
    const badge = ok
      ? `<span class="badge">${gen.charts.length} charts</span>`
      : `<span class="badge err">${gen.error || "n/a"}</span>`;
    div.innerHTML = `<h3>${gen.name} ${badge}</h3>`;

    if (ok) {
      div.appendChild(buildTable(gen.charts));
      if (gen.folder) {
        const rel = gen.folder.split(/output[\\/]+web[\\/]+/).pop().replace(/\\/g, "/");
        const a = document.createElement("a");
        a.className = "dl";
        a.href = `/download_zip?dir=${encodeURIComponent(rel)}`;
        a.textContent = "Download playable folder (.zip)";
        div.appendChild(a);
      }
    }
    host.appendChild(div);
  });
}

function buildTable(charts) {
  const cols = [
    ["difficulty", "Difficulty"],
    ["nps", "NPS"],
    ["onset_align_ms", "Sync (ms)"],
    ["onset_recall", "Recall"],
    ["lane_imbalance", "Lane imb."],
    ["crossover_rate", "Crossover"],
    ["candle_rate", "Candle/min"],
  ];
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr>" + cols.map(([, h]) => `<th>${h}</th>`).join("") + "</tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  charts.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = cols
      .map(([k]) => `<td>${fmt(c[k])}</td>`)
      .join("");
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
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

loadCapabilities();

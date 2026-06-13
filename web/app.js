"use strict";

const $ = (sel) => document.querySelector(sel);

let pollTimer = null;

async function loadCapabilities() {
  try {
    const caps = await fetch("/api/capabilities").then((r) => r.json());
    const box = $("#difficulties");
    box.innerHTML = "";
    const defaults = new Set(["Beginner", "Easy", "Medium", "Hard"]);
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
    } else if (meta.error) {
      msg.textContent = "Couldn't auto-detect: " + meta.error +
        ". Fill in manually.";
      msg.className = "hint err";
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
function renderResults(job) {
  $("#results").classList.remove("hidden");

  // The server always returns {is_playlist, title, songs:[...]}; tolerate an
  // older single-PipelineResult shape just in case.
  const songs = job.songs || [job];
  const isPlaylist = !!job.is_playlist && songs.length > 1;

  const host = $("#generators");
  host.innerHTML = "";

  if (isPlaylist) {
    $("#song-info").innerHTML =
      `<strong>${escapeHtml(job.title)}</strong> &mdash; playlist &middot; ` +
      `${songs.length} songs`;
    host.appendChild(buildPackDownloads(songs));
    songs.forEach((s, i) => host.appendChild(buildSongSection(s, i)));
  } else {
    const song = songs[0];
    $("#song-info").innerHTML =
      `<strong>${escapeHtml(song.title)}</strong> &mdash; ` +
      `${escapeHtml(song.artist)} &middot; BPM ${song.bpm} &middot; ` +
      `${song.duration}s`;
    renderSong(host, song);
  }

  host.appendChild(buildLegend());
}

// Render one song's summary + generator cards (single-song view).
function renderSong(host, song) {
  const working = (song.generators || []).filter(
    (g) => !g.error && g.charts && g.charts.length
  );
  host.appendChild(buildSummary(working));
  (song.generators || []).forEach((gen) => {
    host.appendChild(buildGeneratorCard(gen));
  });
}

// One section per song inside a playlist (metrics only; downloads are the
// per-generator packs shown at the top).
function buildSongSection(song, idx) {
  const box = document.createElement("div");
  box.className = "song-section";
  if (song.error) {
    box.innerHTML =
      `<h3 class="song-title">${idx + 1}. ${escapeHtml(song.title)}</h3>` +
      `<p class="muted">Failed: ${escapeHtml(song.error)}</p>`;
    return box;
  }
  const head = document.createElement("h3");
  head.className = "song-title";
  head.innerHTML =
    `${idx + 1}. ${escapeHtml(song.title)} ` +
    `<span class="muted">&middot; BPM ${song.bpm} &middot; ${song.duration}s</span>`;
  box.appendChild(head);
  (song.generators || []).forEach((gen) => {
    box.appendChild(buildGeneratorCard(gen, true));
  });
  return box;
}

// One download per generator pack (each pack holds every song's folder).
function buildPackDownloads(songs) {
  const box = document.createElement("div");
  box.className = "summary";
  box.innerHTML =
    "<h3>Download packs</h3><p class='muted'>Each .zip is a ready " +
    "StepMania pack \u2014 unzip it into your <code>Songs</code> folder and " +
    "every track shows up in its own folder.</p>";

  const seen = new Set();
  const ul = document.createElement("ul");
  ul.className = "plain";
  songs.forEach((s) =>
    (s.generators || []).forEach((g) => {
      if (!g.folder) return;
      const rel = g.folder
        .split(/output[\\/]+web[\\/]+/)
        .pop()
        .replace(/\\/g, "/");
      if (seen.has(rel)) return;
      seen.add(rel);
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.className = "dl";
      a.href = `/download_zip?dir=${encodeURIComponent(rel)}`;
      a.textContent = `\u2b07 ${g.name} \u2014 all ${songs.length} songs (.zip)`;
      li.appendChild(a);
      ul.appendChild(li);
    })
  );
  box.appendChild(ul);
  return box;
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

// Short description shown under each generator's heading.
function genDescription(name) {
  if (/temposync|footgraph/i.test(name)) {
    return "Our custom generator \u2014 reads the real audio (BPM, beats, " +
      "onsets) and picks foot-friendly steps for a dance pad.";
  }
  if (/autostepper/i.test(name)) {
    return "Open-source baseline (Java). Fully automatic; included only " +
      "as a comparison reference.";
  }
  return "";
}

function buildGeneratorCard(gen, hideDownload) {
  const div = document.createElement("div");
  div.className = "gen";
  const ok = !gen.error && gen.charts && gen.charts.length;
  const desc = genDescription(gen.name);
  const descHtml = desc ? `<p class="gen-desc">${escapeHtml(desc)}</p>` : "";

  if (!ok) {
    div.innerHTML =
      `<h3>${escapeHtml(gen.name)} ` +
      `<span class="badge err">couldn't generate</span></h3>` +
      descHtml +
      `<p class="muted">${escapeHtml(gen.error || "not available")}</p>`;
    return div;
  }

  div.innerHTML =
    `<h3>${escapeHtml(gen.name)} ` +
    `<span class="badge">${gen.charts.length} charts</span></h3>` +
    descHtml;
  div.appendChild(buildTable(gen.charts));

  if (gen.folder && !hideDownload) {
    const rel = gen.folder
      .split(/output[\\/]+web[\\/]+/)
      .pop()
      .replace(/\\/g, "/");
    const a = document.createElement("a");
    a.className = "dl";
    a.href = `/download_zip?dir=${encodeURIComponent(rel)}`;
    a.textContent = "\u2b07 Download StepMania pack (.zip)";
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
    playlist: $("#playlist").checked,
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

/* Dashboard behaviour: fetch JSON from /api and render ECharts. Dark theme tokens come from CSS. */
(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const SERIES = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--series-${i}`));
  const OTHER = css("--text-muted");
  const fmt = new Intl.NumberFormat("en-GB");
  const compact = new Intl.NumberFormat("en-GB", { notation: "compact", maximumSignificantDigits: 4 });
  const params = new URLSearchParams(location.search);
  const charts = [];

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status} ${url}`);
    return res.json();
  }

  function chart(id) {
    const el = document.getElementById(id);
    const c = echarts.init(el, null, { renderer: "canvas" });
    charts.push(c);
    return c;
  }
  window.addEventListener("resize", () => charts.forEach((c) => c.resize()));

  function base(extra) {
    const muted = css("--text-muted"), grid = css("--grid"), axis = css("--axis");
    return Object.assign({
      backgroundColor: "transparent",
      textStyle: { color: muted, fontFamily: css("--font") },
      grid: { left: 24, right: 24, top: 24, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis", backgroundColor: css("--surface-2"), borderColor: css("--border"),
        textStyle: { color: css("--text-primary") }, axisPointer: { type: "line", lineStyle: { color: axis } },
        valueFormatter: (v) => (v == null ? "—" : fmt.format(v)),
      },
      xAxis: { type: "category", axisLine: { lineStyle: { color: axis } }, axisTick: { show: false },
               axisLabel: { color: muted, hideOverlap: true } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: grid } }, axisLabel: { color: muted, formatter: (v) => compact.format(v) } },
    }, extra);
  }

  function empty(c, message) {
    c.clear();
    c.setOption({ title: { text: message || "No data for this range", left: "center", top: "middle",
      textStyle: { color: css("--text-muted"), fontSize: 13, fontWeight: 400 } } });
  }

  function segmented(id, fallback, onChange) {
    const el = document.getElementById(id);
    let value = params.get(id) || fallback;
    const paint = () => el.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.value === value)));
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      value = b.dataset.value; paint(); onChange();
    });
    paint();
    return () => value;
  }

  function syncURL(obj) {
    const p = new URLSearchParams(obj);
    history.replaceState(null, "", `${location.pathname}?${p}`);
  }

  const shortTime = (t) => t.slice(0, 16).replace("T", " ");

  // Skill name -> RuneScape Wiki icon URL, rendered by the server from reference.SKILL_ICONS.
  let SKILL_ICONS = {};
  try { SKILL_ICONS = JSON.parse(document.body.dataset.skillIcons || "{}"); } catch (e) { /* no icons */ }
  function skillIcon(name) {
    const url = SKILL_ICONS[name];
    if (!url) return null;
    const img = new Image(18, 18);
    Object.assign(img, { className: "skill-icon", src: url, alt: "", loading: "lazy", referrerPolicy: "no-referrer" });
    img.addEventListener("error", () => img.remove());
    return img;
  }
  const setIcon = (id, name) => document.getElementById(id).replaceChildren(...[skillIcon(name)].filter(Boolean));

  // ------------------------------------------------------------------ XP page
  function xpPage() {
    const skill = document.getElementById("skill");
    const bucket = document.getElementById("bucket");
    skill.value = params.get("skill") || "-1";
    bucket.value = params.get("bucket") || "day";
    const range = segmented("range", params.get("range") || "7d", load);
    const cSeries = chart("chart-series"), cGains = chart("chart-gains"), cTop = chart("chart-top");
    skill.addEventListener("change", load);
    bucket.addEventListener("change", load);

    async function load() {
      const s = skill.value, r = range(), b = bucket.value;
      const name = skill.options[skill.selectedIndex].text;
      syncURL({ skill: s, range: r, bucket: b });
      document.getElementById("export").href = `/api/export/xp.csv?skill=${s}&range=${r}`;
      document.getElementById("series-title").textContent = `${name} XP`;
      document.getElementById("gains-title").textContent = `${name} XP gained per ${b}`;
      ["skill-icon", "series-icon", "gains-icon"].forEach((id) => setIcon(id, name));
      const [series, gains, top, stats] = await Promise.all([
        getJSON(`/api/xp/series?skill=${s}&range=${r}`),
        getJSON(`/api/xp/gains?skill=${s}&range=${r}&bucket=${b}`),
        getJSON(`/api/xp/by-skill?range=${r}`),
        getJSON(`/api/xp/stats?skill=${s}&range=${r}`),
      ]);
      document.getElementById("st-gained").textContent = fmt.format(stats.gained);
      document.getElementById("st-rate").textContent = stats.xp_per_hour == null ? "—" : fmt.format(Math.round(stats.xp_per_hour));
      document.getElementById("st-best").textContent = stats.best_day ? fmt.format(stats.best_day.xp) : "—";
      document.getElementById("st-best-date").textContent = stats.best_day ? stats.best_day.bucket : "";
      document.getElementById("st-streak").textContent = `${stats.streak_days} d`;

      if (!series.length) empty(cSeries);
      else cSeries.setOption(base({
        xAxis: Object.assign(base().xAxis, { data: series.map((p) => shortTime(p.t)), boundaryGap: false }),
        yAxis: Object.assign(base().yAxis, { scale: true }),
        series: [{ name: `${name} XP`, type: "line", data: series.map((p) => p.xp), showSymbol: series.length < 40,
                   symbolSize: 8, lineStyle: { width: 2, color: SERIES[0] }, itemStyle: { color: SERIES[0] } }],
      }), true);

      if (!gains.length) empty(cGains);
      else cGains.setOption(base({
        legend: { data: ["XP gained", "7-period average"], textStyle: { color: css("--text-secondary") }, top: 0 },
        grid: { left: 24, right: 24, top: 32, bottom: 8, containLabel: true },
        xAxis: Object.assign(base().xAxis, { data: gains.map((g) => g.bucket.replace("T", " ")) }),
        series: [
          { name: "XP gained", type: "bar", data: gains.map((g) => g.xp), barMaxWidth: 24,
            itemStyle: { color: SERIES[0], borderRadius: [4, 4, 0, 0] } },
          { name: "7-period average", type: "line", data: gains.map((g) => g.ma7), showSymbol: false,
            lineStyle: { width: 2, color: SERIES[1] }, itemStyle: { color: SERIES[1] } },
        ],
      }), true);

      const gained = top.filter((t) => t.xp > 0).slice(0, 15).reverse();
      // Axis labels draw the wiki icon through ECharts rich text; a failed image just leaves a gap.
      const rich = {};
      gained.forEach((t, i) => { if (SKILL_ICONS[t.skill]) rich[`i${i}`] = { width: 16, height: 16, backgroundColor: { image: SKILL_ICONS[t.skill] } }; });
      if (!gained.length) empty(cTop);
      else cTop.setOption(base({
        tooltip: Object.assign(base().tooltip, { axisPointer: { type: "shadow" } }),
        // containLabel ignores rich-text images, so leave room for the 16px icon plus its gap.
        grid: { left: 30, right: 56, top: 8, bottom: 8, containLabel: true },
        xAxis: { type: "value", splitLine: { lineStyle: { color: css("--grid") } }, axisLabel: { color: css("--text-muted"), formatter: (v) => compact.format(v) } },
        yAxis: { type: "category", data: gained.map((t) => t.skill), axisTick: { show: false },
                 axisLine: { lineStyle: { color: css("--axis") } }, axisLabel: { color: css("--text-secondary"), rich,
                   formatter: (v, i) => (rich[`i${i}`] ? `{i${i}|}  ${v}` : v) } },
        series: [{ name: "XP gained", type: "bar", data: gained.map((t) => t.xp), barMaxWidth: 18,
                   itemStyle: { color: SERIES[0], borderRadius: [0, 4, 4, 0] },
                   label: { show: true, position: "right", color: css("--text-secondary"), formatter: (p) => compact.format(p.value) } }],
      }), true);
    }
    load();
  }

  // ------------------------------------------------------------------ Boss page
  function bossPage() {
    const boss = document.getElementById("boss");
    const bucket = document.getElementById("bucket");
    bucket.value = params.get("bucket") || "day";
    const range = segmented("range", params.get("range") || "30d", load);
    const cKills = chart("chart-kills"), cCum = chart("chart-cumulative");
    boss.addEventListener("change", load);
    bucket.addEventListener("change", load);
    let populated = false;

    async function load() {
      const r = range(), b = bucket.value;
      const bossParam = boss.value ? `&boss=${encodeURIComponent(boss.value)}` : "";
      syncURL(Object.assign({ range: r, bucket: b }, boss.value ? { boss: boss.value } : {}));
      const [kills, totals] = await Promise.all([
        getJSON(`/api/bosses/kills?range=${r}&bucket=${b}${bossParam}`),
        getJSON(`/api/bosses/totals?range=${r}`),
      ]);
      if (!populated) {
        kills.all_bosses.forEach((name) => boss.add(new Option(name, name)));
        if (params.get("boss")) { boss.value = params.get("boss"); populated = true; return load(); }
        populated = true;
      }
      const colour = (s) => (s.name === "Other" ? OTHER : SERIES[s.slot % SERIES.length]);
      if (!kills.buckets.length) { empty(cKills); empty(cCum); }
      else {
        cKills.setOption(base({
          legend: { type: "scroll", top: 0, textStyle: { color: css("--text-secondary") } },
          grid: { left: 24, right: 24, top: 36, bottom: 8, containLabel: true },
          tooltip: Object.assign(base().tooltip, { axisPointer: { type: "shadow" } }),
          xAxis: Object.assign(base().xAxis, { data: kills.buckets }),
          yAxis: Object.assign(base().yAxis, { minInterval: 1, axisLabel: { color: css("--text-muted") } }),
          series: kills.series.map((s) => ({
            name: s.name, type: "bar", stack: "kills", data: s.data.map((v) => v || null), barMaxWidth: 28,
            itemStyle: { color: colour(s), borderColor: css("--surface-1"), borderWidth: 1 },
          })),
        }), true);
        const perBucket = kills.buckets.map((_, i) => kills.series.reduce((sum, s) => sum + s.data[i], 0));
        let run = 0;
        const cumulative = perBucket.map((v) => (run += v));
        cCum.setOption(base({
          xAxis: Object.assign(base().xAxis, { data: kills.buckets, boundaryGap: false }),
          yAxis: Object.assign(base().yAxis, { minInterval: 1 }),
          series: [{ name: boss.value || "All bosses", type: "line", step: "end", data: cumulative, showSymbol: false,
                     lineStyle: { width: 2, color: SERIES[0] }, itemStyle: { color: SERIES[0] },
                     areaStyle: { color: SERIES[0], opacity: 0.12 } }],
        }), true);
      }
      const tbody = document.querySelector("#totals tbody");
      tbody.replaceChildren(...(totals.length ? totals : [{ boss: "No kills in this range", kills: "" }]).map((t) => {
        const tr = document.createElement("tr");
        const a = document.createElement("td");
        a.textContent = t.boss + (t.known === false ? " (unrecognised)" : "");
        const n = document.createElement("td");
        n.className = "num"; n.textContent = t.kills === "" ? "" : fmt.format(t.kills);
        tr.append(a, n);
        return tr;
      }));
    }
    load();
  }

  // ------------------------------------------------------------------ Events page
  function eventsPage() {
    const els = Object.fromEntries(["type", "subject", "start", "end"].map((k) => [k, document.getElementById(k)]));
    for (const k in els) if (params.get(k)) els[k].value = params.get(k);
    const PAGE = 100;
    const SKILL_SUBJECTS = new Set(["level_up", "xp_milestone"]);
    let offset = 0;
    async function load() {
      const p = new URLSearchParams();
      for (const k in els) if (els[k].value) p.set(k, els[k].value);
      syncURL(p);
      document.getElementById("export").href = `/api/export/events.csv${els.type.value ? `?type=${els.type.value}` : ""}`;
      p.set("limit", PAGE); p.set("offset", offset);
      const rows = await getJSON(`/api/events?${p}`);
      const tbody = document.querySelector("#events tbody");
      tbody.replaceChildren(...rows.map((r) => {
        const tr = document.createElement("tr");
        const cells = [
          [shortTime(r.time), "num"], [r.event_type, "tag"], [r.subject + (r.known_subject ? "" : " ?"), ""],
          [r.count, "num"], [r.value == null ? "" : fmt.format(r.value), "num"], [`${r.text} — ${r.details}`, "muted"],
        ];
        cells.forEach(([v, cls], i) => {
          const td = document.createElement("td");
          if (cls === "tag") { const s = document.createElement("span"); s.className = "tag"; s.textContent = v; td.append(s); }
          else if (i === 2 && SKILL_SUBJECTS.has(r.event_type) && SKILL_ICONS[r.subject]) {
            const s = document.createElement("span"); s.className = "skill"; s.append(skillIcon(r.subject), v); td.append(s);
          }
          else { td.textContent = v; if (cls) td.className = cls; }
          if (i === 5) td.title = `raw #${r.raw_id}, first seen ${shortTime(r.first_seen)}, parser ${r.parser_version}`;
          tr.append(td);
        });
        return tr;
      }));
      if (!rows.length) tbody.innerHTML = '<tr><td colspan="6" class="muted">No events match.</td></tr>';
      document.getElementById("pageinfo").textContent = `Rows ${offset + 1}–${offset + rows.length}`;
      document.getElementById("prev").disabled = offset === 0;
      document.getElementById("next").disabled = rows.length < PAGE;
    }
    document.getElementById("apply").addEventListener("click", () => { offset = 0; load(); });
    document.getElementById("prev").addEventListener("click", () => { offset = Math.max(0, offset - PAGE); load(); });
    document.getElementById("next").addEventListener("click", () => { offset += PAGE; load(); });
    load();
  }

  window.Tracker = { xpPage, bossPage, eventsPage };
})();

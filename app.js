// Static public dashboard script for French Electricity Load Forecast

(function () {
  "use strict";

  const ACCENT_COLOR = "#1f4e9c";
  const RTE_COLOR = "#888888";
  const ACTUAL_COLOR = "#111111"; // Near-black for actual consumption
  const BAND_COLOR = "rgba(31, 78, 156, 0.15)"; // 15% opacity for interval band
  const BAND_LEGEND_COLOR = "rgba(31, 78, 156, 0.35)";
  const REPLAY_AREA_COLOR = "rgba(245, 158, 11, 0.08)";
  const MODEL_REPLAY_BAR = "#7ba4df"; // Lighter accent for replay bars
  const RTE_REPLAY_BAR = "#cccccc"; // Lighter grey for replay bars

  function formatMw(val) {
    if (val === null || val === undefined || isNaN(val)) return "-";
    return val.toLocaleString("en-US") + " MW";
  }

  function formatTime(isoStr) {
    if (!isoStr) return "-";
    try {
      const d = new Date(isoStr);
      return d.toLocaleString("en-GB", {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Europe/Paris",
      });
    } catch (e) {
      return isoStr;
    }
  }

  function formatDeliveryDate(dateStr) {
    if (!dateStr) return "";
    try {
      const parts = dateStr.split("-").map(Number);
      const d = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2], 12, 0, 0));
      return d.toLocaleDateString("en-GB", {
        weekday: "short",
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      }).replace(",", "");
    } catch (e) {
      return dateStr;
    }
  }

  function isTomorrowInParis(dateStr) {
    if (!dateStr) return false;
    try {
      const nowParisStr = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Europe/Paris",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date());
      const parts = nowParisStr.split("-").map(Number);
      const todayParis = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
      const tomorrowParis = new Date(todayParis.getTime() + 86400000);
      return dateStr === tomorrowParis.toISOString().slice(0, 10);
    } catch (e) {
      return false;
    }
  }

  function getReplayIntervals(records, dateKey) {
    const replayDates = [];
    records.forEach(r => {
      if (r.is_replay) {
        replayDates.push(r[dateKey]);
      }
    });
    if (replayDates.length === 0) return [];

    const uniqueSorted = Array.from(new Set(replayDates)).sort();
    const intervals = [];
    let start = uniqueSorted[0];
    let prev = uniqueSorted[0];

    for (let i = 1; i < uniqueSorted.length; i++) {
      const cur = uniqueSorted[i];
      const prevDate = new Date(prev);
      const curDate = new Date(cur);
      const diffDays = Math.round((curDate - prevDate) / (1000 * 60 * 60 * 24));
      if (diffDays > 1) {
        intervals.push([start, prev]);
        start = cur;
      }
      prev = cur;
    }
    intervals.push([start, prev]);
    return intervals;
  }

  function createReplayMarkArea(intervals) {
    if (!intervals || intervals.length === 0) return null;
    const pieces = intervals.map(pair => [
      {
        name: "Replay",
        xAxis: pair[0],
        itemStyle: { color: REPLAY_AREA_COLOR },
        label: {
          show: true,
          position: "insideTopLeft",
          color: "#b45309",
          fontSize: 10,
          fontWeight: "bold",
          formatter: "REPLAY",
        },
      },
      { xAxis: pair[1] },
    ]);
    return { data: pieces, silent: true };
  }

  function renderTomorrowChart(domId, tomorrowData) {
    const el = document.getElementById(domId);
    if (!el || !tomorrowData || !tomorrowData.slots || tomorrowData.slots.length === 0) {
      if (el) el.innerHTML = '<div style="padding: 40px; text-align: center; color: #888;">No forecast available for tomorrow.</div>';
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = el.clientWidth <= 450 || window.innerWidth <= 450;
    const slots = tomorrowData.slots;
    const times = slots.map(s => s.time);
    const points = slots.map(s => s.forecast_mw);
    const rtes = slots.map(s => s.rte_mw);
    const lowers = slots.map(s => s.lower_mw);

    // Compute diff for interval band
    const diffs = slots.map(s => {
      if (s.lower_mw !== null && s.upper_mw !== null) {
        return s.upper_mw - s.lower_mw;
      }
      return null;
    });

    // Mark tomorrow peak with value and time
    const validSlots = slots.filter(s => s.forecast_mw !== null);
    const peakSlot = validSlots.length > 0
      ? validSlots.reduce((max, s) => (s.forecast_mw > max.forecast_mw ? s : max), validSlots[0])
      : null;

    const hasRte = slots.some(s => s.rte_mw !== null && s.rte_mw !== undefined);
    const rteNoteEl = document.getElementById("tomorrow-rte-note");
    if (rteNoteEl) {
      rteNoteEl.style.display = hasRte ? "none" : "inline-flex";
    }

    const series = [
      {
        name: "Lower Bound (Hidden)",
        type: "line",
        data: lowers,
        lineStyle: { opacity: 0 },
        stack: "band",
        symbol: "none",
        silent: true,
      },
      {
        name: "80% Conformal Interval",
        type: "line",
        data: diffs,
        lineStyle: { opacity: 0 },
        areaStyle: { color: BAND_COLOR },
        stack: "band",
        symbol: "none",
      },
      {
        name: "Model Forecast",
        type: "line",
        data: points,
        itemStyle: { color: ACCENT_COLOR },
        lineStyle: { width: 2.5, color: ACCENT_COLOR },
        symbol: "none",
        markPoint: peakSlot ? {
          symbol: "circle",
          symbolSize: 8,
          itemStyle: { color: ACCENT_COLOR, borderColor: "#ffffff", borderWidth: 2 },
          label: {
            show: true,
            position: "top",
            distance: 8,
            formatter: `Peak: ${peakSlot.forecast_mw.toLocaleString()} MW (${peakSlot.time})`,
            backgroundColor: "#ffffff",
            borderColor: ACCENT_COLOR,
            borderWidth: 1,
            borderRadius: 4,
            padding: [4, 7],
            color: ACCENT_COLOR,
            fontWeight: "bold",
            fontSize: 11,
          },
          data: [{ coord: [peakSlot.time, peakSlot.forecast_mw], value: peakSlot.forecast_mw }],
        } : undefined,
        z: 3,
      },
    ];

    const legendData = [
      { name: "Model Forecast" },
      {
        name: "80% Conformal Interval",
        icon: "rect",
        itemStyle: { color: BAND_LEGEND_COLOR, borderColor: ACCENT_COLOR, borderWidth: 1 },
      },
    ];

    if (hasRte) {
      legendData.push({ name: "RTE D-1" });
      series.push({
        name: "RTE D-1",
        type: "line",
        data: rtes,
        itemStyle: { color: RTE_COLOR },
        lineStyle: { width: 1.5, type: "dashed", color: RTE_COLOR },
        symbol: "none",
        z: 2,
      });
    }

    const option = {
      animation: false,
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const idx = params[0].dataIndex;
          const s = slots[idx];
          let out = `<div style="font-weight: bold; margin-bottom: 4px;">Slot ${s.time} Paris</div>`;
          if (s.forecast_mw !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: bold;">Model:</span> ${s.forecast_mw.toLocaleString()} MW</div>`;
          }
          if (s.lower_mw !== null && s.upper_mw !== null) {
            out += `<div style="color:#555;">80% Interval: [${s.lower_mw.toLocaleString()}, ${s.upper_mw.toLocaleString()}] MW</div>`;
          }
          if (s.rte_mw !== null && s.rte_mw !== undefined) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: bold;">RTE D-1:</span> ${s.rte_mw.toLocaleString()} MW</div>`;
          }
          return out;
        },
      },
      legend: {
        data: legendData,
        top: 0,
        textStyle: { fontSize: 12 },
      },
      grid: {
        left: "3%",
        right: "6%",
        bottom: "8%",
        top: "18%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: times,
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#ccc" } },
        axisLabel: {
          color: "#666",
          hideOverlap: true,
          formatter: function (val, idx) {
            const step = isMobile ? 12 : 6;
            return idx % step === 0 ? val : "";
          },
        },
      },
      yAxis: {
        type: "value",
        scale: true,
        name: "Power (MW)",
        nameTextStyle: { color: "#666", fontSize: 11 },
        axisLine: { show: false },
        splitLine: { lineStyle: { color: "#eee" } },
        axisLabel: { color: "#666" },
      },
      series: series,
    };

    chart.setOption(option);
    return chart;
  }

  function renderRecent7dChart(domId, recentSlots) {
    const el = document.getElementById(domId);
    if (!el || !recentSlots || recentSlots.length === 0) {
      if (el) el.innerHTML = '<div style="padding: 40px; text-align: center; color: #888;">No recent 7-day history recorded.</div>';
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = el.clientWidth <= 450 || window.innerWidth <= 450;
    const times = recentSlots.map(s => s.time);
    const actuals = recentSlots.map(s => s.actual_mw);
    const forecasts = recentSlots.map(s => s.forecast_mw);
    const rtes = recentSlots.map(s => s.rte_mw);

    const replayIntervals = getReplayIntervals(recentSlots, "time");
    const markArea = createReplayMarkArea(replayIntervals);

    const option = {
      animation: false,
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const idx = params[0].dataIndex;
          const s = recentSlots[idx];
          let out = `<div style="font-weight: bold; margin-bottom: 4px;">${s.time}</div>`;
          if (s.is_replay) {
            out += `<div style="color: #b45309; font-weight: bold; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          if (s.actual_mw !== null) {
            out += `<div><span style="color:${ACTUAL_COLOR}; font-weight: bold;">Actual:</span> ${s.actual_mw.toLocaleString()} MW</div>`;
          }
          if (s.forecast_mw !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: bold;">Model:</span> ${s.forecast_mw.toLocaleString()} MW`;
            if (s.actual_mw !== null) {
              const diff = s.forecast_mw - s.actual_mw;
              out += ` (${diff > 0 ? "+" : ""}${diff.toLocaleString()} MW)`;
            }
            out += `</div>`;
          }
          if (s.rte_mw !== null) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: bold;">RTE D-1:</span> ${s.rte_mw.toLocaleString()} MW`;
            if (s.actual_mw !== null) {
              const rDiff = s.rte_mw - s.actual_mw;
              out += ` (${rDiff > 0 ? "+" : ""}${rDiff.toLocaleString()} MW)`;
            }
            out += `</div>`;
          }
          return out;
        },
      },
      legend: {
        data: ["Actual", "Model Forecast", "RTE D-1"],
        top: 0,
        textStyle: { fontSize: 12 },
      },
      dataZoom: [
        {
          type: "slider",
          start: 57, // Opens on the last 3 days (4/7 ~ 57%)
          end: 100,
          bottom: 6,
          height: 24,
          borderColor: "#e5e7eb",
          textStyle: { color: "#666", fontSize: 11 },
        },
        {
          type: "inside",
          start: 57,
          end: 100,
        },
      ],
      grid: {
        left: "3%",
        right: "3%",
        bottom: "14%",
        top: "14%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: times,
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#ccc" } },
        axisLabel: {
          color: "#666",
          hideOverlap: true,
          formatter: function (val) {
            if (isMobile) {
              return val.endsWith("00:00") ? val.slice(5, 10) : "";
            }
            if (val.endsWith("00:00") || val.endsWith("12:00")) {
              return val.slice(5);
            }
            return "";
          },
        },
      },
      yAxis: {
        type: "value",
        scale: true,
        name: "Power (MW)",
        nameTextStyle: { color: "#666", fontSize: 11 },
        axisLine: { show: false },
        splitLine: { lineStyle: { color: "#eee" } },
        axisLabel: { color: "#666" },
      },
      series: [
        {
          name: "Actual",
          type: "line",
          data: actuals,
          itemStyle: { color: ACTUAL_COLOR },
          lineStyle: { width: 2, color: ACTUAL_COLOR },
          symbol: "none",
          markArea: markArea,
          z: 3,
        },
        {
          name: "Model Forecast",
          type: "line",
          data: forecasts,
          itemStyle: { color: ACCENT_COLOR },
          lineStyle: { width: 2, color: ACCENT_COLOR },
          symbol: "none",
          z: 2,
        },
        {
          name: "RTE D-1",
          type: "line",
          data: rtes,
          itemStyle: { color: RTE_COLOR },
          lineStyle: { width: 1.5, color: RTE_COLOR, type: "dashed" },
          symbol: "none",
          z: 1,
        },
      ],
    };

    chart.setOption(option);
    return chart;
  }

  function renderDailyMae28dChart(domId, dailyMaeItems, vintageSwitchDate) {
    const el = document.getElementById(domId);
    if (!el || !dailyMaeItems || dailyMaeItems.length === 0) {
      if (el) el.innerHTML = '<div style="padding: 40px; text-align: center; color: #888;">No daily MAE records available.</div>';
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = el.clientWidth <= 450 || window.innerWidth <= 450;
    const dates = dailyMaeItems.map(d => d.date.slice(5)); // 'MM-DD'
    const fullDates = dailyMaeItems.map(d => d.date);

    const modelData = dailyMaeItems.map(d => ({
      value: d.model_mae,
      is_replay: d.is_replay,
      date: d.date,
    }));
    const rteData = dailyMaeItems.map(d => ({
      value: d.rte_mae,
      is_replay: d.is_replay,
      date: d.date,
    }));

    const hasReplay = dailyMaeItems.some(d => d.is_replay);

    let switchX = null;
    let switchBeforeFirst = false;

    let switchDate = vintageSwitchDate || null;
    if (!switchDate) {
      for (let i = 1; i < dailyMaeItems.length; i++) {
        const prev = dailyMaeItems[i - 1].actual_vintage;
        const cur = dailyMaeItems[i].actual_vintage;
        if (prev === "consolidated" && (cur === "realtime" || cur === "real-time")) {
          switchDate = dailyMaeItems[i].date;
          break;
        }
      }
      if (!switchDate && dailyMaeItems.length > 0) {
        const allRealtime = dailyMaeItems.every(d => d.actual_vintage === "realtime" || d.actual_vintage === "real-time");
        if (allRealtime) {
          switchBeforeFirst = true;
        }
      }
    }

    if (switchDate && dailyMaeItems.length > 0) {
      if (switchDate < dailyMaeItems[0].date) {
        switchBeforeFirst = true;
      } else {
        const matchIdx = dailyMaeItems.findIndex(d => d.date === switchDate);
        if (matchIdx >= 0) {
          switchX = dates[matchIdx];
        }
      }
    }

    if (switchBeforeFirst) {
      switchX = null;
    }

    const descEl = document.getElementById("daily-mae-desc");
    if (descEl) {
      const baseDesc = hasReplay
        ? "Daily MAE for Model (blue) vs RTE D-1 (grey). Lighter bars denote replay days."
        : "Daily MAE for Model (blue) vs RTE D-1 (grey).";
      if (switchBeforeFirst) {
        descEl.textContent = `${baseDesc} All days on real-time actuals (provisional).`;
      } else {
        descEl.textContent = baseDesc;
      }
    }

    const markLine = switchX ? {
      silent: true,
      symbol: "none",
      data: [
        {
          xAxis: switchX,
          lineStyle: {
            color: "#b45309",
            type: "dashed",
            width: 1.5,
          },
          label: {
            show: true,
            rotate: 0,
            formatter: isMobile ? "real-time actuals\n(provisional)" : "real-time actuals (provisional)",
            position: "insideEndTop",
            distance: 6,
            align: "left",
            verticalAlign: "top",
            color: "#b45309",
            backgroundColor: "rgba(254, 243, 199, 0.95)",
            borderColor: "#fde68a",
            borderWidth: 1,
            borderRadius: 3,
            padding: [2, 5],
            fontSize: isMobile ? 10 : 11,
            fontWeight: "bold",
            lineHeight: 14,
          },
        },
      ],
    } : undefined;

    const option = {
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const idx = params[0].dataIndex;
          const d = dailyMaeItems[idx];
          let out = `<div style="font-weight: bold; margin-bottom: 4px;">${d.date}</div>`;
          if (d.is_replay) {
            out += `<div style="color: #b45309; font-weight: bold; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          if (d.model_mae !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: bold;">Model MAE:</span> ${d.model_mae.toLocaleString()} MW</div>`;
          }
          if (d.rte_mae !== null) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: bold;">RTE D-1 MAE:</span> ${d.rte_mae.toLocaleString()} MW</div>`;
          }
          if (d.model_mae !== null && d.rte_mae !== null) {
            const gap = d.model_mae - d.rte_mae;
            const pct = d.rte_mae > 0 ? ((d.rte_mae - d.model_mae) / d.rte_mae) * 100 : 0;
            const color = gap <= 0 ? "#16a34a" : "#dc2626";
            out += `<div style="color:${color}; font-weight: bold;">Difference: ${gap > 0 ? "+" : ""}${gap.toLocaleString()} MW (${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%)</div>`;
          }
          return out;
        },
      },
      legend: {
        data: ["Model Daily MAE", "RTE D-1 Daily MAE"],
        top: 0,
        textStyle: { fontSize: 12 },
      },
      grid: {
        left: "3%",
        right: "3%",
        bottom: "8%",
        top: "16%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#ccc" } },
        axisLabel: {
          color: "#666",
          hideOverlap: true,
          interval: function (idx) {
            const step = isMobile ? 7 : 4;
            return idx % step === 0;
          },
        },
      },
      yAxis: {
        type: "value",
        min: 0,
        name: "MAE (MW)",
        nameTextStyle: { color: "#666", fontSize: 11 },
        axisLine: { show: false },
        splitLine: { lineStyle: { color: "#eee" } },
        axisLabel: { color: "#666" },
      },
      series: [
        {
          name: "Model Daily MAE",
          type: "bar",
          color: ACCENT_COLOR,
          data: modelData,
          barGap: "20%",
          markLine: markLine,
          itemStyle: {
            color: function (params) {
              return params.data.is_replay ? MODEL_REPLAY_BAR : ACCENT_COLOR;
            },
          },
        },
        {
          name: "RTE D-1 Daily MAE",
          type: "bar",
          color: RTE_COLOR,
          data: rteData,
          itemStyle: {
            color: function (params) {
              return params.data.is_replay ? RTE_REPLAY_BAR : RTE_COLOR;
            },
          },
        },
      ],
    };

    chart.setOption(option);
    return chart;
  }

  function renderRollingMaeChart(domId, history, vintageSwitchDate) {
    const el = document.getElementById(domId);
    if (!el || !history || history.length === 0) {
      if (el) el.innerHTML = '<div style="padding: 40px; text-align: center; color: #888;">No rolling MAE data available.</div>';
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = el.clientWidth <= 450 || window.innerWidth <= 450;
    const dates = history.map(r => r.date);
    const modelRolling = history.map(r => r.model_rolling_mae);
    const rteRolling = history.map(r => r.rte_rolling_mae);

    const replayIntervals = getReplayIntervals(history, "date");
    const markArea = createReplayMarkArea(replayIntervals);

    let switchDate = vintageSwitchDate || null;
    if (!switchDate) {
      for (let i = 1; i < history.length; i++) {
        const prev = history[i - 1].actual_vintage;
        const cur = history[i].actual_vintage;
        if (prev === "consolidated" && (cur === "realtime" || cur === "real-time")) {
          switchDate = history[i].date;
          break;
        }
      }
    }

    const markLine = (switchDate && dates.includes(switchDate)) ? {
      silent: true,
      symbol: "none",
      data: [
        {
          xAxis: switchDate,
          lineStyle: {
            color: "#b45309",
            type: "dashed",
            width: 1.5,
          },
          label: {
            show: true,
            rotate: 0,
            formatter: isMobile ? "real-time actuals\n(provisional)" : "real-time actuals (provisional)",
            position: "insideEndTop",
            distance: 6,
            align: "left",
            verticalAlign: "top",
            color: "#b45309",
            backgroundColor: "rgba(254, 243, 199, 0.95)",
            borderColor: "#fde68a",
            borderWidth: 1,
            borderRadius: 3,
            padding: [2, 5],
            fontSize: isMobile ? 10 : 11,
            fontWeight: "bold",
            lineHeight: 14,
          },
        },
      ],
    } : undefined;

    const rollingDescEl = document.getElementById("rolling-mae-desc");
    if (rollingDescEl) {
      const baseDesc = history.some(r => r.is_replay)
        ? "Trailing 28-day MAE for the model (blue) versus RTE D-1 (grey). Shaded background denotes replay data."
        : "Trailing 28-day MAE for the model (blue) versus RTE D-1 (grey).";
      if (switchDate && dates.length > 0 && switchDate < dates[0]) {
        rollingDescEl.textContent = `${baseDesc} All days on real-time actuals (provisional).`;
      } else {
        rollingDescEl.textContent = baseDesc;
      }
    }

    const option = {
      animation: false,
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const idx = params[0].dataIndex;
          const r = history[idx];
          let out = `<div style="font-weight: bold; margin-bottom: 4px;">${r.date}</div>`;
          if (r.is_replay) {
            out += `<div style="color: #b45309; font-weight: bold; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          if (r.model_rolling_mae !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: bold;">Model 28d MAE:</span> ${r.model_rolling_mae.toLocaleString()} MW</div>`;
          }
          if (r.rte_rolling_mae !== null) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: bold;">RTE 28d MAE:</span> ${r.rte_rolling_mae.toLocaleString()} MW</div>`;
          }
          if (r.model_rolling_mae !== null && r.rte_rolling_mae !== null) {
            const gap = r.model_rolling_mae - r.rte_rolling_mae;
            out += `<div style="color: ${gap <= 0 ? "#16a34a" : "#dc2626"}; font-weight: bold;">Difference: ${gap > 0 ? "+" : ""}${gap.toLocaleString()} MW</div>`;
          }
          return out;
        },
      },
      legend: {
        data: ["Model 28d MAE", "RTE 28d MAE"],
        top: 0,
        textStyle: { fontSize: 12 },
      },
      grid: {
        left: "3%",
        right: "3%",
        bottom: "8%",
        top: "16%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#ccc" } },
        axisLabel: {
          color: "#666",
          hideOverlap: true,
          interval: function (idx) {
            const step = isMobile ? 20 : 10;
            return idx % step === 0;
          },
          formatter: function (val) {
            return val.slice(5);
          },
        },
      },
      yAxis: {
        type: "value",
        scale: true,
        name: "MAE (MW)",
        nameTextStyle: { color: "#666", fontSize: 11 },
        axisLine: { show: false },
        splitLine: { lineStyle: { color: "#eee" } },
        axisLabel: { color: "#666" },
      },
      series: [
        {
          name: "Model 28d MAE",
          type: "line",
          data: modelRolling,
          itemStyle: { color: ACCENT_COLOR },
          lineStyle: { width: 2, color: ACCENT_COLOR },
          symbol: "none",
          markArea: markArea,
          markLine: markLine,
        },
        {
          name: "RTE 28d MAE",
          type: "line",
          data: rteRolling,
          itemStyle: { color: RTE_COLOR },
          lineStyle: { width: 1.8, color: RTE_COLOR, type: "dashed" },
          symbol: "none",
        },
      ],
    };

    chart.setOption(option);
    return chart;
  }

  function renderCusumChart(domId, cusumData) {
    const el = document.getElementById(domId);
    if (!el || !cusumData || !cusumData.series || cusumData.series.length === 0) {
      if (el) el.innerHTML = '<div style="padding: 40px; text-align: center; color: #888;">No consolidated CUSUM drift statistics available.</div>';
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = el.clientWidth <= 450 || window.innerWidth <= 450;
    const seriesData = cusumData.series;
    const dates = seriesData.map(r => r.date);
    const sPlus = seriesData.map(r => r.s_plus);
    const sMinus = seriesData.map(r => r.s_minus);
    const h = cusumData.h || 11.46;

    const replayIntervals = getReplayIntervals(seriesData, "date");
    const markArea = createReplayMarkArea(replayIntervals);

    const option = {
      animation: false,
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const idx = params[0].dataIndex;
          const r = seriesData[idx];
          let out = `<div style="font-weight: bold; margin-bottom: 4px;">${r.date} (Consolidated)</div>`;
          if (r.is_replay) {
            out += `<div style="color: #b45309; font-weight: bold; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          out += `<div><span style="color:${ACCENT_COLOR}; font-weight: bold;">S+ (Positive Drift):</span> ${r.s_plus.toFixed(2)}</div>`;
          out += `<div><span style="color:#d97706; font-weight: bold;">S- (Negative Drift):</span> ${r.s_minus.toFixed(2)}</div>`;
          out += `<div style="color:#666; font-size: 11px;">Threshold h = ${h.toFixed(2)}</div>`;
          if (r.alarm) {
            out += `<div style="color: #dc2626; font-weight: bold; margin-top: 4px;">ALARM TRIGGERED (${r.event})</div>`;
          }
          return out;
        },
      },
      legend: {
        data: ["S+ (Positive)", "S- (Negative)"],
        top: 0,
        textStyle: { fontSize: 12 },
      },
      grid: {
        left: 10,
        right: "3%",
        bottom: "8%",
        top: "16%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#ccc" } },
        axisLabel: {
          color: "#666",
          hideOverlap: true,
          interval: function (idx) {
            const step = isMobile ? 20 : 10;
            return idx % step === 0;
          },
          formatter: function (val) {
            return val.slice(5);
          },
        },
      },
      yAxis: {
        type: "value",
        scale: true,
        name: "Drift (std)",
        nameTextStyle: { color: "#666", fontSize: 11 },
        axisLine: { show: false },
        splitLine: { lineStyle: { color: "#eee" } },
        axisLabel: { color: "#666" },
        min: 0,
        max: function (val) {
          return Math.max(Math.ceil((val.max || 0) * 1.15), Math.ceil(h + 1));
        },
      },
      series: [
        {
          name: "S+ (Positive)",
          type: "line",
          data: sPlus,
          itemStyle: { color: ACCENT_COLOR },
          lineStyle: { width: 1.8, color: ACCENT_COLOR },
          symbol: "none",
          markArea: markArea,
          markLine: {
            silent: true,
            symbol: "none",
            data: [
              {
                yAxis: h,
                lineStyle: { color: "#dc2626", type: "dashed", width: 1.5 },
                label: { formatter: `h = ${h.toFixed(2)}`, position: "insideEndTop", color: "#dc2626" },
              },
            ],
          },
        },
        {
          name: "S- (Negative)",
          type: "line",
          data: sMinus,
          itemStyle: { color: "#d97706" },
          lineStyle: { width: 1.8, color: "#d97706" },
          symbol: "none",
        },
      ],
    };

    chart.setOption(option);
    return chart;
  }

  function initDashboard() {
    fetch("data/latest.json")
      .then(resp => {
        if (!resp.ok) {
          throw new Error("Failed to load data: status " + resp.status);
        }
        return resp.json();
      })
      .then(data => {
        if (!data || data.status === "empty" || !data.tomorrow) {
          document.getElementById("empty-state").style.display = "block";
          document.getElementById("content-section").style.display = "none";
          document.getElementById("meta-updated").textContent = formatTime(data ? data.updated_at : null);
          return;
        }

        document.getElementById("meta-updated").textContent = formatTime(data.updated_at);

        // Header Tiles
        const tiles = data.header_tiles || {};

        // Forecast delivery date title and Peak label
        if (data.tomorrow && data.tomorrow.delivery_day) {
          const delivDay = data.tomorrow.delivery_day;
          const formattedDate = formatDeliveryDate(delivDay);
          const isTom = isTomorrowInParis(delivDay);
          const titleText = isTom
            ? `Tomorrow's Forecast (${formattedDate})`
            : `Forecast for ${formattedDate}`;
          const titleEl = document.getElementById("forecast-card-title");
          if (titleEl) titleEl.textContent = titleText;
          const peakLabel = document.getElementById("tile-peak-label");
          if (peakLabel) {
            peakLabel.textContent = isTom ? "Tomorrow Peak" : "Peak Forecast";
          }
        }

        // Tomorrow Peak
        if (tiles.tomorrow_peak && tiles.tomorrow_peak.peak_mw !== null) {
          document.getElementById("tile-peak-mw").textContent = formatMw(tiles.tomorrow_peak.peak_mw);
          document.getElementById("tile-peak-sub").textContent =
            `at ${tiles.tomorrow_peak.peak_time} Paris (${tiles.tomorrow_peak.delivery_day})`;
          if (tiles.tomorrow_peak.is_replay) {
            document.getElementById("tomorrow-badge").style.display = "inline-block";
          }
        }

        // Rolling 28d MAE with relative gain
        if (tiles.rolling_28d_mae && tiles.rolling_28d_mae.model_mw !== null) {
          const m = tiles.rolling_28d_mae.model_mw;
          const r = tiles.rolling_28d_mae.rte_mw;
          const gap = tiles.rolling_28d_mae.gap_mw;
          let gain = tiles.rolling_28d_mae.relative_gain_pct;
          if (gain === undefined || gain === null) {
            gain = r > 0 ? Math.round(((r - m) / r) * 1000) / 10 : 0;
          }
          document.getElementById("tile-mae-val").textContent =
            `${m.toLocaleString()} vs ${r.toLocaleString()} MW`;
          const diffSign = gap <= 0 ? `${gap.toLocaleString()} MW` : `+${gap.toLocaleString()} MW`;
          const gainSign = gain >= 0 ? `+${gain.toFixed(1)}%` : `${gain.toFixed(1)}%`;
          let subText = `Diff: ${diffSign} (${gainSign} relative gain)`;
          if (tiles.rolling_28d_mae.is_provisional) {
            subText += " \u2022 provisional";
            const provBadge = document.getElementById("tile-mae-provisional");
            if (provBadge) provBadge.style.display = "inline-block";
          }
          document.getElementById("tile-mae-sub").textContent = subText;
        }

        // Beating RTE share
        if (tiles.beating_rte && tiles.beating_rte.pct !== null) {
          document.getElementById("tile-beating-val").textContent = `${tiles.beating_rte.pct}%`;
          let subText = `${tiles.beating_rte.better_days} of ${tiles.beating_rte.total_days} days beating RTE D-1`;
          if (tiles.beating_rte.is_provisional) {
            subText += " \u2022 provisional";
            const provBadge = document.getElementById("tile-beating-provisional");
            if (provBadge) provBadge.style.display = "inline-block";
          }
          document.getElementById("tile-beating-sub").textContent = subText;
        }

        // Delivery counts
        if (tiles.delivery_counts) {
          const c = tiles.delivery_counts;
          document.getElementById("tile-deliveries-val").textContent = `${c.served} served`;
          document.getElementById("tile-deliveries-sub").textContent =
            `${c.fallback} fallback, ${c.none} none, ${c.replay} replay`;
          if (c.replay > 0) {
            document.getElementById("replay-banner").style.display = "flex";
          }
        }

        // Render charts
        const activeCharts = [];

        // 1. Tomorrow Forecast
        const c1 = renderTomorrowChart("chart-tomorrow", data.tomorrow);
        if (c1) activeCharts.push(c1);

        // 2. Last 7 Days Actual vs Forecast (with dataZoom slider)
        const recentSlots = (data.recent_7d && data.recent_7d.length > 0) ? data.recent_7d : (data.recent_14d || []);
        const c2 = renderRecent7dChart("chart-recent7d", recentSlots);
        if (c2) activeCharts.push(c2);

        // 3. Daily MAE Grouped Bar Chart (Last 28 Days)
        const dailyMaeItems = (data.daily_mae_28d && data.daily_mae_28d.length > 0)
          ? data.daily_mae_28d
          : (data.rolling_mae_history ? data.rolling_mae_history.slice(-28) : []);
        const c3 = renderDailyMae28dChart("chart-daily-mae-28d", dailyMaeItems, data.vintage_switch_date);
        if (c3) activeCharts.push(c3);

        // 4. Rolling 28-Day MAE
        const c4 = renderRollingMaeChart("chart-rolling-mae", data.rolling_mae_history, data.vintage_switch_date);
        if (c4) activeCharts.push(c4);

        // 5. CUSUM Drift Monitoring (Consolidated)
        const c5 = renderCusumChart("chart-cusum", data.cusum_history);
        if (c5) activeCharts.push(c5);

        window.addEventListener("resize", () => {
          activeCharts.forEach(ch => ch.resize());
        });
      })
      .catch(err => {
        console.error("Failed to load dashboard data:", err);
        document.getElementById("empty-state").style.display = "block";
        document.getElementById("meta-updated").textContent = "Unavailable";
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDashboard);
  } else {
    initDashboard();
  }
})();

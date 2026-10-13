// Public results page: fills the figures and draws the charts from data/latest.json.

(function () {
  "use strict";

  const ACCENT_COLOR = "#5b8def";
  const RTE_COLOR = "#b4bac4";
  const ACTUAL_COLOR = "#f2f4f7";
  const BAND_COLOR = "rgba(91, 141, 239, 0.2)";
  const BAND_LEGEND_COLOR = "rgba(91, 141, 239, 0.45)";
  const REPLAY_AREA_COLOR = "rgba(255, 255, 255, 0.045)";
  const REPLAY_LABEL_COLOR = "#9aa3af";
  const MARKER_COLOR = "#f0a35e";
  const ALARM_COLOR = "#ff7b72";
  const NEGATIVE_COLOR = "#e3b341";
  const MODEL_REPLAY_BAR = "#9db9f5";
  const RTE_REPLAY_BAR = "#dfe3e9";
  const TEXT_COLOR = "#d6dbe3";
  const FONT = '"IBM Plex Sans", system-ui, sans-serif';

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function fmt(n) {
    return n.toLocaleString("en-US");
  }

  function formatMw(val) {
    if (val === null || val === undefined || isNaN(val)) return "-";
    return fmt(val) + " MW";
  }

  function formatTime(isoStr) {
    if (!isoStr) return "-";
    try {
      return new Date(isoStr).toLocaleString("en-GB", {
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
      const p = dateStr.split("-").map(Number);
      return new Date(Date.UTC(p[0], p[1] - 1, p[2], 12)).toLocaleDateString("en-GB", {
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

  function shortDate(dateStr) {
    const p = dateStr.split("-").map(Number);
    return `${p[2]} ${MONTHS[p[1] - 1]}`;
  }

  function isTomorrowInParis(dateStr) {
    if (!dateStr) return false;
    try {
      const nowParis = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Europe/Paris",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date());
      const p = nowParis.split("-").map(Number);
      const tomorrow = new Date(Date.UTC(p[0], p[1] - 1, p[2]) + 86400000);
      return dateStr === tomorrow.toISOString().slice(0, 10);
    } catch (e) {
      return false;
    }
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function show(id, visible) {
    const el = document.getElementById(id);
    if (el) el.hidden = !visible;
  }

  function placeholder(el, message) {
    if (el) el.innerHTML = `<p style="padding: 48px 0; text-align: center; color: #9aa3af;">${message}</p>`;
  }

  function isRealtime(vintage) {
    return vintage === "realtime" || vintage === "real-time";
  }

  function getReplayIntervals(records, dateKey) {
    const dates = Array.from(new Set(records.filter(r => r.is_replay).map(r => r[dateKey]))).sort();
    if (dates.length === 0) return [];
    const intervals = [];
    let start = dates[0];
    let prev = dates[0];
    for (let i = 1; i < dates.length; i++) {
      const gapDays = Math.round((new Date(dates[i]) - new Date(prev)) / 86400000);
      if (gapDays > 1) {
        intervals.push([start, prev]);
        start = dates[i];
      }
      prev = dates[i];
    }
    intervals.push([start, prev]);
    return intervals;
  }

  function createReplayMarkArea(intervals) {
    if (!intervals || intervals.length === 0) return null;
    return {
      silent: true,
      data: intervals.map(pair => [
        {
          xAxis: pair[0],
          itemStyle: { color: REPLAY_AREA_COLOR },
          label: {
            show: true,
            position: "insideTopLeft",
            color: REPLAY_LABEL_COLOR,
            fontSize: 10,
            fontWeight: 600,
            formatter: "replay",
          },
        },
        { xAxis: pair[1] },
      ]),
    };
  }

  // Where actuals switch from consolidated to real-time values. A switch before the first
  // bar draws no marker: the chart note says all days are on real-time actuals instead.
  function vintageSwitch(items, dateOf, payloadSwitchDate) {
    const dates = items.map(dateOf);
    let switchDate = payloadSwitchDate || null;
    let allRealtime = false;
    if (!switchDate) {
      for (let i = 1; i < items.length; i++) {
        if (items[i - 1].actual_vintage === "consolidated" && isRealtime(items[i].actual_vintage)) {
          switchDate = dates[i];
          break;
        }
      }
      if (!switchDate && items.length > 0) {
        allRealtime = items.every(d => isRealtime(d.actual_vintage));
      }
    }
    const beforeFirst = allRealtime || (switchDate !== null && dates.length > 0 && switchDate < dates[0]);
    const marker = switchDate && !beforeFirst && dates.includes(switchDate) ? switchDate : null;
    return { marker, beforeFirst };
  }

  function createVintageMarkLine(markerX, isMobile) {
    if (!markerX) return undefined;
    return {
      silent: true,
      symbol: "none",
      data: [
        {
          xAxis: markerX,
          lineStyle: { color: MARKER_COLOR, type: "dashed", width: 1.5 },
          label: {
            show: true,
            rotate: 0,
            formatter: isMobile ? "real-time actuals\n(provisional)" : "real-time actuals (provisional)",
            position: "insideEndTop",
            distance: 6,
            align: "left",
            verticalAlign: "top",
            color: MARKER_COLOR,
            backgroundColor: "#171b22",
            borderColor: MARKER_COLOR,
            borderWidth: 1,
            borderRadius: 2,
            padding: [2, 5],
            fontSize: isMobile ? 10 : 11,
            fontWeight: 600,
            lineHeight: 14,
          },
        },
      ],
    };
  }

  // Dark theme shared by every chart: transparent canvas, tooltip on a dark panel, light legend text.
  function baseOption(overrides) {
    const option = Object.assign({
      animation: false,
      backgroundColor: "transparent",
      textStyle: { fontFamily: FONT, color: TEXT_COLOR },
    }, overrides);
    option.tooltip = Object.assign({
      backgroundColor: "#171b22",
      borderColor: "#2f3742",
      borderWidth: 1,
      textStyle: { color: TEXT_COLOR, fontSize: 12 },
      extraCssText: "box-shadow: none;",
    }, overrides.tooltip);
    if (overrides.legend) {
      option.legend = Object.assign({}, overrides.legend, {
        textStyle: Object.assign({ color: "#c3cad4" }, overrides.legend.textStyle),
      });
    }
    return option;
  }

  function valueAxis(name, extra) {
    return Object.assign({
      type: "value",
      name: name,
      nameTextStyle: { color: "#9aa3af", fontSize: 11, align: "left" },
      axisLine: { show: false },
      splitLine: { lineStyle: { color: "#232a34" } },
      axisLabel: { color: "#9aa3af" },
    }, extra);
  }

  function isMobileEl(el) {
    return el.clientWidth <= 450 || window.innerWidth <= 450;
  }

  function sameDayTitle(tomorrowData) {
    const dateText = formatDeliveryDate(tomorrowData.delivery_day);
    return isTomorrowInParis(tomorrowData.delivery_day)
      ? `Tomorrow's forecast (${dateText})`
      : `Forecast for ${dateText}`;
  }

  function renderTomorrowChart(domId, tomorrowData) {
    const el = document.getElementById(domId);
    if (!el || !tomorrowData || !tomorrowData.slots || tomorrowData.slots.length === 0) {
      placeholder(el, "No forecast available for tomorrow.");
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = isMobileEl(el);
    const slots = tomorrowData.slots;
    const times = slots.map(s => s.time);
    const points = slots.map(s => s.forecast_mw);
    const rtes = slots.map(s => s.rte_mw);
    const lowers = slots.map(s => s.lower_mw);
    const diffs = slots.map(s =>
      s.lower_mw !== null && s.upper_mw !== null ? s.upper_mw - s.lower_mw : null);

    const validSlots = slots.filter(s => s.forecast_mw !== null);
    const peakSlot = validSlots.length > 0
      ? validSlots.reduce((max, s) => (s.forecast_mw > max.forecast_mw ? s : max), validSlots[0])
      : null;

    const hasRte = slots.some(s => s.rte_mw !== null && s.rte_mw !== undefined);
    show("tomorrow-rte-note", !hasRte);

    if (peakSlot) {
      setText("tomorrow-finding",
        `Demand peaks at ${peakSlot.time} Paris time, at ${formatMw(peakSlot.forecast_mw)}.`);
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
          itemStyle: { color: ACCENT_COLOR, borderColor: "#0f1217", borderWidth: 2 },
          label: {
            show: true,
            position: "top",
            distance: 8,
            formatter: `Peak: ${fmt(peakSlot.forecast_mw)} MW (${peakSlot.time})`,
            backgroundColor: "#171b22",
            borderColor: ACCENT_COLOR,
            borderWidth: 1,
            borderRadius: 2,
            padding: [4, 7],
            color: ACCENT_COLOR,
            fontWeight: 600,
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

    chart.setOption(baseOption({
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const s = slots[params[0].dataIndex];
          let out = `<div style="font-weight: 600; margin-bottom: 4px;">Slot ${s.time} Paris</div>`;
          if (s.forecast_mw !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: 600;">Model:</span> ${fmt(s.forecast_mw)} MW</div>`;
          }
          if (s.lower_mw !== null && s.upper_mw !== null) {
            out += `<div style="color:#9aa3af;">80% Interval: [${fmt(s.lower_mw)}, ${fmt(s.upper_mw)}] MW</div>`;
          }
          if (s.rte_mw !== null && s.rte_mw !== undefined) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: 600;">RTE D-1:</span> ${fmt(s.rte_mw)} MW</div>`;
          }
          return out;
        },
      },
      legend: { data: legendData, top: 0, textStyle: { fontSize: 12 } },
      grid: { left: "3%", right: "6%", bottom: "8%", top: isMobile ? 76 : "18%", containLabel: true },
      xAxis: {
        type: "category",
        data: times,
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#3a424e" } },
        axisLabel: {
          color: "#9aa3af",
          hideOverlap: true,
          formatter: function (val, idx) {
            const step = isMobile ? 12 : 6;
            return idx % step === 0 ? val : "";
          },
        },
      },
      yAxis: valueAxis("Power (MW)", { scale: true }),
      series: series,
    }));
    return chart;
  }

  function maeOver(slots) {
    let model = 0;
    let rte = 0;
    let n = 0;
    slots.forEach(s => {
      if (s.actual_mw !== null && s.forecast_mw !== null && s.rte_mw !== null) {
        model += Math.abs(s.forecast_mw - s.actual_mw);
        rte += Math.abs(s.rte_mw - s.actual_mw);
        n += 1;
      }
    });
    return n > 0 ? { model: Math.round(model / n), rte: Math.round(rte / n) } : null;
  }

  function renderRecent7dChart(domId, recentSlots) {
    const el = document.getElementById(domId);
    if (!el || !recentSlots || recentSlots.length === 0) {
      placeholder(el, "No recent 7-day history recorded.");
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = isMobileEl(el);
    const times = recentSlots.map(s => s.time);
    const markArea = createReplayMarkArea(getReplayIntervals(recentSlots, "time"));

    const mae = maeOver(recentSlots);
    if (mae) {
      const verdict = mae.model < mae.rte ? "below" : mae.model > mae.rte ? "above" : "equal to";
      setText("title-recent7d",
        `Over the last 7 days my error was ${verdict} RTE's: ${fmt(mae.model)} MW against ${fmt(mae.rte)} MW`);
    }

    chart.setOption(baseOption({
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const s = recentSlots[params[0].dataIndex];
          let out = `<div style="font-weight: 600; margin-bottom: 4px;">${s.time}</div>`;
          if (s.is_replay) {
            out += `<div style="color: ${REPLAY_LABEL_COLOR}; font-weight: 600; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          if (s.actual_mw !== null) {
            out += `<div><span style="color:${ACTUAL_COLOR}; font-weight: 600;">Actual:</span> ${fmt(s.actual_mw)} MW</div>`;
          }
          if (s.forecast_mw !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: 600;">Model:</span> ${fmt(s.forecast_mw)} MW`;
            if (s.actual_mw !== null) {
              const diff = s.forecast_mw - s.actual_mw;
              out += ` (${diff > 0 ? "+" : ""}${fmt(diff)} MW)`;
            }
            out += `</div>`;
          }
          if (s.rte_mw !== null) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: 600;">RTE D-1:</span> ${fmt(s.rte_mw)} MW`;
            if (s.actual_mw !== null) {
              const rDiff = s.rte_mw - s.actual_mw;
              out += ` (${rDiff > 0 ? "+" : ""}${fmt(rDiff)} MW)`;
            }
            out += `</div>`;
          }
          return out;
        },
      },
      legend: { data: ["Actual", "Model Forecast", "RTE D-1"], top: 0, textStyle: { fontSize: 12 } },
      dataZoom: [
        { type: "slider", start: 57, end: 100, bottom: 6, height: 24, borderColor: "#2f3742", backgroundColor: "rgba(255, 255, 255, 0.03)", fillerColor: "rgba(91, 141, 239, 0.22)", dataBackground: { lineStyle: { color: "#4b5563" }, areaStyle: { color: "rgba(255, 255, 255, 0.06)" } }, selectedDataBackground: { lineStyle: { color: ACCENT_COLOR }, areaStyle: { color: "rgba(91, 141, 239, 0.25)" } }, handleStyle: { color: "#d6dbe3", borderColor: "#2f3742" }, moveHandleStyle: { color: "#4b5563" }, textStyle: { color: "#9aa3af", fontSize: 11 } },
        { type: "inside", start: 57, end: 100 },
      ],
      grid: { left: "3%", right: "3%", bottom: "16%", top: isMobile ? 76 : "14%", containLabel: true },
      xAxis: {
        type: "category",
        data: times,
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#3a424e" } },
        axisLabel: {
          color: "#9aa3af",
          hideOverlap: true,
          formatter: function (val) {
            if (isMobile) return val.endsWith("00:00") ? val.slice(5, 10) : "";
            return val.endsWith("00:00") || val.endsWith("12:00") ? val.slice(5) : "";
          },
        },
      },
      yAxis: valueAxis("Power (MW)", { scale: true }),
      series: [
        {
          name: "Actual",
          type: "line",
          data: recentSlots.map(s => s.actual_mw),
          itemStyle: { color: ACTUAL_COLOR },
          lineStyle: { width: 2, color: ACTUAL_COLOR },
          symbol: "none",
          markArea: markArea,
          z: 3,
        },
        {
          name: "Model Forecast",
          type: "line",
          data: recentSlots.map(s => s.forecast_mw),
          itemStyle: { color: ACCENT_COLOR },
          lineStyle: { width: 2, color: ACCENT_COLOR },
          symbol: "none",
          z: 2,
        },
        {
          name: "RTE D-1",
          type: "line",
          data: recentSlots.map(s => s.rte_mw),
          itemStyle: { color: RTE_COLOR },
          lineStyle: { width: 1.5, color: RTE_COLOR, type: "dashed" },
          symbol: "none",
          z: 1,
        },
      ],
    }));
    return chart;
  }

  function renderDailyMae28dChart(domId, items, vintageSwitchDate) {
    const el = document.getElementById(domId);
    if (!el || !items || items.length === 0) {
      placeholder(el, "No daily MAE records available.");
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = isMobileEl(el);
    const dates = items.map(d => d.date.slice(5));
    const modelData = items.map(d => ({ value: d.model_mae, is_replay: d.is_replay, date: d.date }));
    const rteData = items.map(d => ({ value: d.rte_mae, is_replay: d.is_replay, date: d.date }));

    const scored = items.filter(d => d.model_mae !== null && d.rte_mae !== null);
    const better = scored.filter(d => d.model_mae < d.rte_mae).length;
    setText("title-daily-mae", `Below RTE on ${better} of the last ${scored.length} days`);

    const vintage = vintageSwitch(items, d => d.date, vintageSwitchDate);
    const markerX = vintage.marker ? vintage.marker.slice(5) : null;

    const notes = [];
    if (items.some(d => d.is_replay)) notes.push("Lighter bars are replay days.");
    if (vintage.beforeFirst) notes.push("All days on real-time actuals (provisional).");
    setText("daily-mae-desc", notes.join(" "));

    chart.setOption(baseOption({
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const d = items[params[0].dataIndex];
          let out = `<div style="font-weight: 600; margin-bottom: 4px;">${d.date}</div>`;
          if (d.is_replay) {
            out += `<div style="color: ${REPLAY_LABEL_COLOR}; font-weight: 600; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          if (d.model_mae !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: 600;">Model MAE:</span> ${fmt(d.model_mae)} MW</div>`;
          }
          if (d.rte_mae !== null) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: 600;">RTE D-1 MAE:</span> ${fmt(d.rte_mae)} MW</div>`;
          }
          if (d.model_mae !== null && d.rte_mae !== null) {
            const gap = d.model_mae - d.rte_mae;
            const pct = d.rte_mae > 0 ? ((d.rte_mae - d.model_mae) / d.rte_mae) * 100 : 0;
            const color = gap <= 0 ? "#5fd08b" : "#ff8a80";
            out += `<div style="color:${color}; font-weight: 600;">Difference: ${gap > 0 ? "+" : ""}${fmt(gap)} MW (${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%)</div>`;
          }
          return out;
        },
      },
      legend: { data: ["Model Daily MAE", "RTE D-1 Daily MAE"], top: 0, textStyle: { fontSize: 12 } },
      grid: { left: "3%", right: "3%", bottom: "8%", top: isMobile ? 64 : "16%", containLabel: true },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#3a424e" } },
        axisLabel: {
          color: "#9aa3af",
          hideOverlap: true,
          interval: function (idx) {
            return idx % (isMobile ? 7 : 4) === 0;
          },
        },
      },
      yAxis: valueAxis("MAE (MW)", { min: 0 }),
      series: [
        {
          name: "Model Daily MAE",
          type: "bar",
          color: ACCENT_COLOR,
          data: modelData,
          barGap: "20%",
          markLine: createVintageMarkLine(markerX, isMobile),
          itemStyle: { color: p => (p.data.is_replay ? MODEL_REPLAY_BAR : ACCENT_COLOR) },
        },
        {
          name: "RTE D-1 Daily MAE",
          type: "bar",
          color: RTE_COLOR,
          data: rteData,
          itemStyle: { color: p => (p.data.is_replay ? RTE_REPLAY_BAR : RTE_COLOR) },
        },
      ],
    }));
    return chart;
  }

  function renderRollingMaeChart(domId, history, vintageSwitchDate) {
    const el = document.getElementById(domId);
    if (!el || !history || history.length === 0) {
      placeholder(el, "No rolling MAE data available.");
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = isMobileEl(el);
    const dates = history.map(r => r.date);
    const markArea = createReplayMarkArea(getReplayIntervals(history, "date"));

    const last = history[history.length - 1];
    if (last.model_rolling_mae !== null && last.rte_rolling_mae !== null) {
      setText("title-rolling-mae",
        `On ${shortDate(last.date)}, the 28-day error was ${fmt(last.model_rolling_mae)} MW for my model and ${fmt(last.rte_rolling_mae)} MW for RTE`);
    }

    const vintage = vintageSwitch(history, r => r.date, vintageSwitchDate);
    const notes = [];
    if (history.some(r => r.is_replay)) notes.push("The shaded part is replay.");
    if (vintage.beforeFirst) notes.push("All days on real-time actuals (provisional).");
    setText("rolling-mae-desc", notes.join(" "));

    chart.setOption(baseOption({
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const r = history[params[0].dataIndex];
          let out = `<div style="font-weight: 600; margin-bottom: 4px;">${r.date}</div>`;
          if (r.is_replay) {
            out += `<div style="color: ${REPLAY_LABEL_COLOR}; font-weight: 600; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          if (r.model_rolling_mae !== null) {
            out += `<div><span style="color:${ACCENT_COLOR}; font-weight: 600;">Model 28d MAE:</span> ${fmt(r.model_rolling_mae)} MW</div>`;
          }
          if (r.rte_rolling_mae !== null) {
            out += `<div><span style="color:${RTE_COLOR}; font-weight: 600;">RTE 28d MAE:</span> ${fmt(r.rte_rolling_mae)} MW</div>`;
          }
          if (r.model_rolling_mae !== null && r.rte_rolling_mae !== null) {
            const gap = r.model_rolling_mae - r.rte_rolling_mae;
            out += `<div style="color: ${gap <= 0 ? "#5fd08b" : "#ff8a80"}; font-weight: 600;">Difference: ${gap > 0 ? "+" : ""}${fmt(gap)} MW</div>`;
          }
          return out;
        },
      },
      legend: { data: ["Model 28d MAE", "RTE 28d MAE"], top: 0, textStyle: { fontSize: 12 } },
      grid: { left: "3%", right: "3%", bottom: "8%", top: isMobile ? 64 : "16%", containLabel: true },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#3a424e" } },
        axisLabel: {
          color: "#9aa3af",
          hideOverlap: true,
          interval: function (idx) {
            return idx % (isMobile ? 20 : 10) === 0;
          },
          formatter: val => val.slice(5),
        },
      },
      yAxis: valueAxis("MAE (MW)", { scale: true }),
      series: [
        {
          name: "Model 28d MAE",
          type: "line",
          data: history.map(r => r.model_rolling_mae),
          itemStyle: { color: ACCENT_COLOR },
          lineStyle: { width: 2, color: ACCENT_COLOR },
          symbol: "none",
          markArea: markArea,
          markLine: createVintageMarkLine(vintage.marker, isMobile),
        },
        {
          name: "RTE 28d MAE",
          type: "line",
          data: history.map(r => r.rte_rolling_mae),
          itemStyle: { color: RTE_COLOR },
          lineStyle: { width: 1.8, color: RTE_COLOR, type: "dashed" },
          symbol: "none",
        },
      ],
    }));
    return chart;
  }

  function renderCusumChart(domId, cusumData) {
    const el = document.getElementById(domId);
    if (!el || !cusumData || !cusumData.series || cusumData.series.length === 0) {
      placeholder(el, "No consolidated CUSUM drift statistics available.");
      return null;
    }

    const chart = echarts.init(el);
    const isMobile = isMobileEl(el);
    const series = cusumData.series;
    const dates = series.map(r => r.date);
    const h = cusumData.h || 11.46;
    const markArea = createReplayMarkArea(getReplayIntervals(series, "date"));

    const alarms = series.filter(r => r.alarm);
    if (alarms.length === 0) {
      setText("title-cusum", `No drift alarm in the last ${series.length} scored days`);
    } else {
      setText("title-cusum", `Drift alarm raised on ${alarms.length} of the last ${series.length} scored days`);
    }
    setText("finding-cusum", "Computed on consolidated actuals only.");

    chart.setOption(baseOption({
      tooltip: {
        trigger: "axis",
        formatter: function (params) {
          if (!params || params.length === 0) return "";
          const r = series[params[0].dataIndex];
          let out = `<div style="font-weight: 600; margin-bottom: 4px;">${r.date} (consolidated)</div>`;
          if (r.is_replay) {
            out += `<div style="color: ${REPLAY_LABEL_COLOR}; font-weight: 600; font-size: 11px;">[REPLAY DATA]</div>`;
          }
          out += `<div><span style="color:${ACCENT_COLOR}; font-weight: 600;">S+:</span> ${r.s_plus.toFixed(2)}</div>`;
          out += `<div><span style="color:${NEGATIVE_COLOR}; font-weight: 600;">S-:</span> ${r.s_minus.toFixed(2)}</div>`;
          out += `<div style="color:#9aa3af; font-size: 11px;">Threshold h = ${h.toFixed(2)}</div>`;
          if (r.alarm) {
            out += `<div style="color: ${ALARM_COLOR}; font-weight: 600; margin-top: 4px;">Alarm (${r.event})</div>`;
          }
          return out;
        },
      },
      legend: { data: ["S+ (Positive)", "S- (Negative)"], top: 0, textStyle: { fontSize: 12 } },
      grid: { left: 10, right: "3%", bottom: "8%", top: isMobile ? 64 : "16%", containLabel: true },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: "#3a424e" } },
        axisLabel: {
          color: "#9aa3af",
          hideOverlap: true,
          interval: function (idx) {
            return idx % (isMobile ? 20 : 10) === 0;
          },
          formatter: val => val.slice(5),
        },
      },
      yAxis: valueAxis("Drift (std)", {
        scale: true,
        min: 0,
        max: function (val) {
          return Math.ceil(Math.max((val.max || 0) * 1.15, h + 1) / 3) * 3;
        },
      }),
      series: [
        {
          name: "S+ (Positive)",
          type: "line",
          data: series.map(r => r.s_plus),
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
                lineStyle: { color: ALARM_COLOR, type: "dashed", width: 1.5 },
                label: { formatter: `h = ${h.toFixed(2)}`, position: "insideEndTop", color: ALARM_COLOR },
              },
            ],
          },
        },
        {
          name: "S- (Negative)",
          type: "line",
          data: series.map(r => r.s_minus),
          itemStyle: { color: NEGATIVE_COLOR },
          lineStyle: { width: 1.8, color: NEGATIVE_COLOR },
          symbol: "none",
        },
      ],
    }));
    return chart;
  }

  // 'Live' once served days exist, 'Replay' while only replayed days are stored.
  function fillStatusPill(counts) {
    const pill = document.getElementById("status-pill");
    if (!pill || !counts) return;
    const live = counts.served > 0;
    if (!live && !(counts.replay > 0)) return;
    pill.textContent = live ? "Live" : "Replay";
    pill.classList.toggle("pill-live", live);
    pill.hidden = false;
  }

  // Thin decorative line of the last 7 days of actual consumption behind the hero text.
  function drawHeroCurve(slots) {
    const svg = document.getElementById("hero-curve");
    const values = (slots || []).map(s => s.actual_mw).filter(v => v !== null && v !== undefined);
    if (!svg || values.length < 2) return;
    const lo = Math.min.apply(null, values);
    const hi = Math.max.apply(null, values);
    const span = hi - lo || 1;
    const points = values.map((v, i) => {
      const x = (i / (values.length - 1)) * 1000;
      const y = 80 - ((v - lo) / span) * 60;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    svg.setAttribute("viewBox", "0 0 1000 100");
    svg.innerHTML = `<polyline points="${points.join(" ")}"></polyline>`;
  }

  function fillFigures(data) {
    const tiles = data.header_tiles || {};
    fillStatusPill(tiles.delivery_counts);

    if (data.tomorrow && data.tomorrow.delivery_day) {
      const tomorrowDate = isTomorrowInParis(data.tomorrow.delivery_day);
      setText("forecast-card-title", sameDayTitle(data.tomorrow));
      setText("tile-peak-label", tomorrowDate ? "Tomorrow's peak" : "Peak forecast");
    }

    if (tiles.tomorrow_peak && tiles.tomorrow_peak.peak_mw !== null) {
      setText("tile-peak-mw", formatMw(tiles.tomorrow_peak.peak_mw));
      setText("tile-peak-sub", `at ${tiles.tomorrow_peak.peak_time} Paris (${tiles.tomorrow_peak.delivery_day})`);
      show("tomorrow-badge", Boolean(tiles.tomorrow_peak.is_replay));
    }

    const mae = tiles.rolling_28d_mae;
    if (mae && mae.model_mw !== null) {
      let gain = mae.relative_gain_pct;
      if (gain === undefined || gain === null) {
        gain = mae.rte_mw > 0 ? Math.round(((mae.rte_mw - mae.model_mw) / mae.rte_mw) * 1000) / 10 : 0;
      }
      setText("tile-mae-val", `${fmt(mae.model_mw)} vs ${fmt(mae.rte_mw)} MW`);
      const diff = mae.gap_mw <= 0 ? `${fmt(mae.gap_mw)} MW` : `+${fmt(mae.gap_mw)} MW`;
      const gainText = gain >= 0 ? `+${gain.toFixed(1)}%` : `${gain.toFixed(1)}%`;
      setText("tile-mae-sub", `Difference ${diff}, ${gainText} relative gain`);
      show("tile-mae-provisional", Boolean(mae.is_provisional));
    }

    const beating = tiles.beating_rte;
    if (beating && beating.pct !== null) {
      setText("tile-beating-val", `${beating.pct}%`);
      setText("tile-beating-sub", `${beating.better_days} of ${beating.total_days} days`);
      show("tile-beating-provisional", Boolean(beating.is_provisional));
    }

    const counts = tiles.delivery_counts;
    if (counts) {
      setText("tile-deliveries-val", `${counts.served} served`);
      setText("tile-deliveries-sub", `${counts.fallback} fallback, ${counts.none} none, ${counts.replay} replay`);
      show("replay-banner", counts.replay > 0);
    }
  }

  function renderCharts(data) {
    const charts = [];
    const recentSlots = (data.recent_7d && data.recent_7d.length > 0) ? data.recent_7d : (data.recent_14d || []);
    const dailyItems = (data.daily_mae_28d && data.daily_mae_28d.length > 0)
      ? data.daily_mae_28d
      : (data.rolling_mae_history ? data.rolling_mae_history.slice(-28) : []);

    [
      renderTomorrowChart("chart-tomorrow", data.tomorrow),
      renderRecent7dChart("chart-recent7d", recentSlots),
      renderDailyMae28dChart("chart-daily-mae-28d", dailyItems, data.vintage_switch_date),
      renderRollingMaeChart("chart-rolling-mae", data.rolling_mae_history, data.vintage_switch_date),
      renderCusumChart("chart-cusum", data.cusum_history),
    ].forEach(c => { if (c) charts.push(c); });

    window.addEventListener("resize", () => charts.forEach(c => c.resize()));
  }

  function hideDataLinks() {
    document.querySelectorAll("[data-needs-data]").forEach(a => { a.hidden = true; });
  }

  // Charts draw on a canvas, so wait for the vendored fonts (or give up after 1.5 s).
  function fontsReady() {
    if (!document.fonts || !document.fonts.load) return Promise.resolve();
    const loaded = Promise.all([
      document.fonts.load('400 12px "IBM Plex Sans"'),
      document.fonts.load('600 12px "IBM Plex Sans"'),
    ]).catch(() => null);
    return Promise.race([loaded, new Promise(resolve => setTimeout(resolve, 1500))]);
  }

  function initPage() {
    fetch("data/latest.json")
      .then(resp => {
        if (!resp.ok) throw new Error("Failed to load data: status " + resp.status);
        return resp.json();
      })
      .then(data => {
        setText("meta-updated", formatTime(data ? data.updated_at : null));
        if (!data || data.status === "empty" || !data.tomorrow) {
          show("empty-state", true);
          show("content-section", false);
          hideDataLinks();
          return null;
        }
        fillFigures(data);
        drawHeroCurve(data.recent_7d && data.recent_7d.length > 0 ? data.recent_7d : data.recent_14d);
        return fontsReady().then(() => renderCharts(data));
      })
      .catch(err => {
        console.error("Failed to load page data:", err);
        show("empty-state", true);
        show("content-section", false);
        hideDataLinks();
        setText("meta-updated", "Unavailable");
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPage);
  } else {
    initPage();
  }
})();

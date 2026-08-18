/**
 * Chart math used by the browser viewer (tui/web_chart.html) and by
 * tests via Node. Keep this file free of DOM / Lightweight Charts APIs
 * so both callers exercise the same functions.
 *
 * Loaded as a classic script it sets globalThis.ChartMath; under Node
 * it is a CommonJS module.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.ChartMath = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Vertical bands as Lightweight Charts scaleMargins. A series occupies
  // [top, 1 - bottom]. These four regions are the layout contract:
  // price and compare share the top band; volume and MACD sit below,
  // non-overlapping. Compare must never use the volume or MACD scale id.
  const PANE_LAYOUT = {
    price:   { scaleId: "right", top: 0.02, bottom: 0.40 },
    volume:  { scaleId: "vol",   top: 0.62, bottom: 0.22 },
    macd:    { scaleId: "macd",  top: 0.82, bottom: 0.02 },
    compare: { scaleId: "cmp",   top: 0.02, bottom: 0.40 },
  };

  function scaleMargins(pane) {
    return { top: pane.top, bottom: pane.bottom };
  }

  // Options for chart.priceScale(id).applyOptions(...). Series-level
  // scaleMargins are ignored by LC v4 for overlay ids; only this path
  // sticks. Overlay axes stay invisible so they cannot steal left/right
  // labels when the user zooms or pans.
  function priceScaleApply(pane) {
    const overlay = pane.scaleId !== "right" && pane.scaleId !== "left";
    return {
      scaleMargins: scaleMargins(pane),
      autoScale: true,
      invertScale: false,
      alignLabels: !overlay,
      visible: !overlay,
      borderVisible: !overlay,
      ticksVisible: !overlay,
      entireTextOnly: false,
      minimumWidth: overlay ? 0 : 64,
    };
  }

  function occupiedRange(pane) {
    return { start: pane.top, end: 1 - pane.bottom };
  }

  function rangesOverlap(a, b, eps) {
    const e = eps == null ? 1e-9 : eps;
    return a.start < b.end - e && b.start < a.end - e;
  }

  function paneLayoutOk(layout) {
    const L = layout || PANE_LAYOUT;
    const price = occupiedRange(L.price);
    const volume = occupiedRange(L.volume);
    const macd = occupiedRange(L.macd);
    const compare = occupiedRange(L.compare);
    if (rangesOverlap(price, volume)) return false;
    if (rangesOverlap(price, macd)) return false;
    if (rangesOverlap(volume, macd)) return false;
    if (rangesOverlap(compare, volume)) return false;
    if (rangesOverlap(compare, macd)) return false;
    if (L.compare.scaleId === L.volume.scaleId) return false;
    if (L.compare.scaleId === L.macd.scaleId) return false;
    return true;
  }

  function sma(values, n) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= n) sum -= values[i - n];
      out.push(i >= n - 1 ? sum / n : null);
    }
    return out;
  }

  function ema(values, n) {
    const k = 2 / (n + 1);
    const out = [];
    let prev = null;
    for (const v of values) {
      prev = prev == null ? v : v * k + prev * (1 - k);
      out.push(prev);
    }
    return out;
  }

  function rollingStd(values, n) {
    const out = [];
    let sum = 0, sumSq = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      sumSq += values[i] * values[i];
      if (i >= n) {
        sum -= values[i - n];
        sumSq -= values[i - n] * values[i - n];
      }
      if (i >= n - 1) {
        const m = sum / n;
        out.push(Math.sqrt(Math.max(0, sumSq / n - m * m)));
      } else {
        out.push(null);
      }
    }
    return out;
  }

  function bollinger(closes, n, k) {
    const period = n == null ? 20 : n;
    const width = k == null ? 2 : k;
    const mid = sma(closes, period);
    const sd = rollingStd(closes, period);
    const up = [];
    const lo = [];
    for (let i = 0; i < closes.length; i++) {
      if (mid[i] == null || sd[i] == null) {
        up.push(null);
        lo.push(null);
      } else {
        up.push(mid[i] + width * sd[i]);
        lo.push(mid[i] - width * sd[i]);
      }
    }
    return { mid: mid, up: up, lo: lo };
  }

  function macd(closes, fast, slow, signal) {
    const f = fast == null ? 12 : fast;
    const s = slow == null ? 26 : slow;
    const g = signal == null ? 9 : signal;
    const eFast = ema(closes, f);
    const eSlow = ema(closes, s);
    const dif = [];
    for (let i = 0; i < closes.length; i++) dif.push(eFast[i] - eSlow[i]);
    const dea = ema(dif, g);
    const hist = [];
    for (let i = 0; i < dif.length; i++) hist.push((dif[i] - dea[i]) * 2);
    return { dif: dif, dea: dea, hist: hist };
  }

  function resample(candlesArr, period) {
    if (!candlesArr || period === "D") return candlesArr ? candlesArr.slice() : [];
    const groups = new Map();
    for (const c of candlesArr) {
      let key;
      if (period === "W") {
        const d = new Date(c.time + "T00:00:00Z");
        const day = (d.getUTCDay() + 6) % 7;  // Mon=0
        key = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(),
                                d.getUTCDate() - day)).toISOString().slice(0, 10);
      } else {
        key = String(c.time).slice(0, 7);
      }
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    const out = [];
    for (const bars of groups.values()) {
      out.push({
        time: bars[bars.length - 1].time,
        open: bars[0].open,
        high: Math.max.apply(null, bars.map(function (b) { return b.high; })),
        low: Math.min.apply(null, bars.map(function (b) { return b.low; })),
        close: bars[bars.length - 1].close,
        volume: bars.reduce(function (s, b) { return s + (b.volume || 0); }, 0),
      });
    }
    return out;
  }

  function timesAreMonotonicUnique(bars) {
    for (let i = 1; i < bars.length; i++) {
      if (String(bars[i].time) <= String(bars[i - 1].time)) return false;
    }
    return true;
  }

  function previousClose(bars, time) {
    if (!bars || !bars.length) return null;
    for (let i = 0; i < bars.length; i++) {
      if (bars[i].time === time) {
        return i > 0 ? bars[i - 1].close : null;
      }
    }
    return null;
  }

  function volumeFromSeriesPoint(point) {
    if (point == null) return null;
    if (typeof point === "number") return Number.isFinite(point) ? point : null;
    if (typeof point.value === "number" && Number.isFinite(point.value)) return point.value;
    return null;
  }

  function legendSnapshot(bars, time, volumePoint, ohlc) {
    if (!ohlc) return null;
    const prev = previousClose(bars, time);
    const vol = volumeFromSeriesPoint(volumePoint);
    const changePct = (prev != null && prev !== 0)
      ? ((ohlc.close - prev) / prev) * 100
      : 0;
    return {
      time: time,
      open: ohlc.open,
      high: ohlc.high,
      low: ohlc.low,
      close: ohlc.close,
      volume: vol,
      changePct: changePct,
    };
  }

  function formatVolume(v) {
    if (v == null || !Number.isFinite(v)) return "-";
    if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(2) + "K";
    return String(Math.round(v));
  }

  function formatLegend(snap) {
    if (!snap) return "";
    const chg = snap.changePct;
    const sign = chg >= 0 ? "+" : "";
    return (
      String(snap.time) +
      "  O " + snap.open.toFixed(2) +
      "  H " + snap.high.toFixed(2) +
      "  L " + snap.low.toFixed(2) +
      "  C " + snap.close.toFixed(2) +
      "  V " + formatVolume(snap.volume) +
      "  " + sign + chg.toFixed(2) + "%"
    );
  }

  function nextBusinessDay(iso) {
    const d = new Date(String(iso) + "T00:00:00Z");
    if (isNaN(d.getTime())) return "";
    for (let n = 0; n < 10; n++) {
      d.setUTCDate(d.getUTCDate() + 1);
      const wd = d.getUTCDay();
      if (wd !== 0 && wd !== 6) return d.toISOString().slice(0, 10);
    }
    return "";
  }

  function forecastPoints(values, dates, lastBar) {
    // lastBar {time, close} stitches the overlay to the last candle and
    // fills missing Kronos dates so the yellow line actually plots.
    const out = [];
    if (!values || !values.length) return out;
    const ds = dates || [];
    let prev = lastBar && lastBar.time ? String(lastBar.time) : "";
    if (prev && lastBar.close != null) {
      out.push({ time: prev, value: lastBar.close });
    }
    for (let i = 0; i < values.length; i++) {
      let t = ds[i];
      if (t == null || t === "") t = prev ? nextBusinessDay(prev) : "";
      if (!t) continue;
      out.push({ time: t, value: values[i] });
      prev = t;
    }
    return out;
  }

  function LoadSeq() {
    this.n = 0;
  }
  LoadSeq.prototype.next = function () {
    this.n += 1;
    return this.n;
  };
  LoadSeq.prototype.isCurrent = function (id) {
    return id === this.n;
  };

  function lineData(base, values) {
    const out = [];
    for (let i = 0; i < values.length; i++) {
      if (values[i] == null || !base[i]) continue;
      out.push({ time: base[i].time, value: values[i] });
    }
    return out;
  }

  // Only /chart/<symbol> preloads. GET / is pathname "/" — a replace of
  // /^\/chart\/?/ would leave "/" and wrongly call load("/").
  function pathSymbol(pathname) {
    const m = String(pathname || "").match(/^\/chart\/(.+)$/);
    if (!m) return "";
    let raw;
    try {
      raw = decodeURIComponent(m[1]);
    } catch (e) {
      return "";
    }
    const s = raw.trim();
    if (!s || s === "/") return "";
    return s;
  }

  function marketOf(symbol) {
    const identity = symbolIdentity(symbol);
    const u = String(identity ? identity.symbol : symbol || "").trim().toUpperCase();
    if (!u) return "";
    if (/\.(SS|SZ|BJ|SH)$/.test(u)) return "CN";
    if (u.endsWith(".HK")) return "HK";
    if (/\.(T|TYO)$/.test(u)) return "JP";
    if (/\.(KS|KQ)$/.test(u)) return "KR";
    return "US";
  }

  // MICs are exchange identities, not broad market aliases. Only suffixes
  // with one deterministic venue are mapped here; a bare US ticker stays
  // unbound because its listing venue cannot be inferred safely.
  function micOf(symbol) {
    const u = String(symbol || "").trim().toUpperCase();
    if (/\.(SS|SH)$/.test(u)) return "XSHG";
    if (u.endsWith(".SZ")) return "XSHE";
    if (u.endsWith(".BJ")) return "XBEI";
    if (u.endsWith(".HK")) return "XHKG";
    if (/\.(T|TYO)$/.test(u)) return "XTKS";
    if (/\.(KS|KQ)$/.test(u)) return "XKRX";
    return "";
  }

  function symbolIdentity(value, fallbackMic) {
    let raw = value;
    let suppliedMic = fallbackMic;
    if (value && typeof value === "object") {
      raw = value.symbol;
      suppliedMic = value.mic == null ? fallbackMic : value.mic;
    }
    raw = String(raw || "").trim();
    suppliedMic = String(suppliedMic || "").trim();
    if (!raw) return null;
    const firstAt = raw.indexOf("@");
    if (firstAt >= 0) {
      if (firstAt !== raw.lastIndexOf("@")) return null;
      const tokenSymbol = raw.slice(0, firstAt).trim();
      const tokenMic = raw.slice(firstAt + 1).trim();
      if (!tokenSymbol || !tokenMic || (suppliedMic && suppliedMic !== tokenMic)) return null;
      raw = tokenSymbol;
      suppliedMic = tokenMic;
    }
    if (!suppliedMic) suppliedMic = micOf(raw);
    if (suppliedMic && suppliedMic !== "crypto" && !/^[A-Z][A-Z0-9]{3}$/.test(suppliedMic)) {
      return null;
    }
    return {
      symbol: raw,
      mic: suppliedMic,
      token: raw + (suppliedMic ? "@" + suppliedMic : ""),
    };
  }

  function sameValidatedReference(left, right) {
    if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
    const li = left.identity || {};
    const ri = right.identity || {};
    const lc = left.calendar || {};
    const rc = right.calendar || {};
    const fields = [
      "schema", "bars_sha256", "output_sha256",
    ];
    if (fields.some(function (key) { return left[key] !== right[key]; })) return false;
    const identityFields = [
      "symbol", "market", "interval", "calendar", "currency", "price_basis", "volume_unit",
    ];
    if (identityFields.some(function (key) { return li[key] !== ri[key]; })) return false;
    return lc.name === rc.name && lc.sessions_sha256 === rc.sessions_sha256;
  }

  function validatedProvenanceLeg(role, symbol, mic, provenance) {
    const identity = symbolIdentity(symbol, mic);
    if (!identity || !provenance || provenance.data_mode !== "validated") return null;
    const digest = /^[0-9a-f]{64}$/;
    if (!digest.test(String(provenance.bars_sha256 || "")) ||
        !digest.test(String(provenance.output_sha256 || ""))) return null;
    const reference = provenance.reference;
    const producer = provenance.identity;
    if (!reference || reference.schema !== "stammtisch.validated-bars-reference.v1" ||
        !producer || typeof producer !== "object") return null;
    if (producer.symbol !== identity.symbol || producer.market !== identity.mic ||
        producer.interval !== "1d") return null;
    if (reference.bars_sha256 !== provenance.bars_sha256 ||
        reference.output_sha256 !== provenance.output_sha256 ||
        !sameValidatedReference(reference, {
          schema: reference.schema,
          bars_sha256: provenance.bars_sha256,
          output_sha256: provenance.output_sha256,
          identity: producer,
          calendar: reference.calendar,
        })) return null;
    const calendar = reference.calendar;
    if (!calendar || calendar.name !== producer.calendar ||
        !digest.test(String(calendar.sessions_sha256 || ""))) return null;
    return {
      role: String(role || "leg"),
      symbol: identity.symbol,
      mic: identity.mic,
      token: identity.token,
      bars_sha256: provenance.bars_sha256,
      output_sha256: provenance.output_sha256,
      identity: producer,
      calendar: calendar,
      reference: reference,
    };
  }

  function provenanceText(legs, derived) {
    if (!Array.isArray(legs) || !legs.length) return "";
    const lines = ["data_mode=validated", "verified_legs=" + legs.length];
    legs.forEach(function (leg, index) {
      const prefix = "leg[" + index + "].";
      lines.push(prefix + "role=" + leg.role);
      lines.push(prefix + "symbol=" + leg.symbol);
      lines.push(prefix + "mic=" + leg.mic);
      lines.push(prefix + "bars_sha256=" + leg.bars_sha256);
      lines.push(prefix + "output_sha256=" + leg.output_sha256);
      lines.push(prefix + "identity=" + JSON.stringify(leg.identity));
      lines.push(prefix + "calendar=" + JSON.stringify(leg.calendar));
      lines.push(prefix + "reference=" + JSON.stringify(leg.reference));
    });
    if (Array.isArray(derived) && derived.length) {
      lines.push("drawn_transforms=" + derived.join(","));
    }
    return lines.join("\n");
  }

  function barTime(t) {
    if (t == null || t === "") return "";
    if (typeof t === "number") return "";
    const s = String(t).trim();
    if (s.length >= 10 && s[4] === "-" && s[7] === "-") {
      const y = Number(s.slice(0, 4));
      const mo = Number(s.slice(5, 7));
      const d = Number(s.slice(8, 10));
      if (!y || mo < 1 || mo > 12 || d < 1 || d > 31) return "";
      return s.slice(0, 10);
    }
    return "";
  }

  // Drop non-finite OHLC, collapse duplicate calendar days, sort so
  // times are strictly unique and ascending. The viewer setData's this
  // series; Lightweight Charts throws on unsorted / duplicate times.
  function sanitizeCandles(rows) {
    const byTime = Object.create(null);
    const src = rows || [];
    for (let i = 0; i < src.length; i++) {
      const row = src[i];
      if (!row) continue;
      const time = barTime(row.time);
      if (!time) continue;
      const open = Number(row.open);
      const high = Number(row.high);
      const low = Number(row.low);
      const close = Number(row.close);
      if (![open, high, low, close].every(Number.isFinite)) continue;
      let volume = Number(row.volume);
      if (!Number.isFinite(volume)) volume = 0;
      if (byTime[time]) {
        const prev = byTime[time];
        prev.high = Math.max(prev.high, high);
        prev.low = Math.min(prev.low, low);
        prev.close = close;
        prev.volume += volume;
      } else {
        byTime[time] = {
          time: time, open: open, high: high, low: low, close: close, volume: volume,
        };
      }
    }
    const times = Object.keys(byTime).sort();
    const out = [];
    for (let i = 0; i < times.length; i++) out.push(byTime[times[i]]);
    return out;
  }

  const HISTORY_KEY = "stammtisch.chart.history";
  const HISTORY_CAP = 24;

  function historyNormalize(entry) {
    if (typeof entry === "string") entry = { symbol: entry };
    if (!entry || typeof entry !== "object") return null;
    const identity = symbolIdentity(entry.symbol, entry.mic);
    if (!identity) return null;
    return {
      symbol: identity.symbol,
      mic: identity.mic,
      token: identity.token,
      market: String(entry.market || marketOf(identity.symbol)).trim(),
      name: String(entry.name || "").trim(),
    };
  }

  function historyParse(raw) {
    if (raw == null || raw === "") return [];
    let src = raw;
    if (typeof raw === "string") {
      try {
        src = JSON.parse(raw);
      } catch (e) {
        return [];
      }
    }
    if (!Array.isArray(src)) return [];
    const out = [];
    const seen = Object.create(null);
    for (let i = 0; i < src.length; i++) {
      const item = historyNormalize(src[i]);
      if (!item || seen[item.token]) continue;
      seen[item.token] = true;
      out.push(item);
    }
    return out.slice(0, HISTORY_CAP);
  }

  function historyDump(list) {
    return JSON.stringify(historyParse(list));
  }

  function historyRecord(list, entry) {
    const item = historyNormalize(entry);
    if (!item) return historyParse(list);
    const rest = historyParse(list).filter(function (h) { return h.token !== item.token; });
    return [item].concat(rest).slice(0, HISTORY_CAP);
  }

  function historyRemove(list, symbol) {
    const identity = symbolIdentity(symbol);
    const token = identity ? identity.token : String(symbol || "").trim();
    return historyParse(list).filter(function (h) { return h.token !== token; });
  }

  return {
    PANE_LAYOUT: PANE_LAYOUT,
    scaleMargins: scaleMargins,
    priceScaleApply: priceScaleApply,
    occupiedRange: occupiedRange,
    rangesOverlap: rangesOverlap,
    paneLayoutOk: paneLayoutOk,
    sma: sma,
    ema: ema,
    rollingStd: rollingStd,
    bollinger: bollinger,
    macd: macd,
    resample: resample,
    timesAreMonotonicUnique: timesAreMonotonicUnique,
    previousClose: previousClose,
    volumeFromSeriesPoint: volumeFromSeriesPoint,
    legendSnapshot: legendSnapshot,
    formatVolume: formatVolume,
    formatLegend: formatLegend,
    forecastPoints: forecastPoints,
    nextBusinessDay: nextBusinessDay,
    LoadSeq: LoadSeq,
    lineData: lineData,
    pathSymbol: pathSymbol,
    marketOf: marketOf,
    micOf: micOf,
    symbolIdentity: symbolIdentity,
    sameValidatedReference: sameValidatedReference,
    validatedProvenanceLeg: validatedProvenanceLeg,
    provenanceText: provenanceText,
    barTime: barTime,
    sanitizeCandles: sanitizeCandles,
    HISTORY_KEY: HISTORY_KEY,
    HISTORY_CAP: HISTORY_CAP,
    historyNormalize: historyNormalize,
    historyParse: historyParse,
    historyDump: historyDump,
    historyRecord: historyRecord,
    historyRemove: historyRemove,
  };
});

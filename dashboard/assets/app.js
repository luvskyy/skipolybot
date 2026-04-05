/* ═══════════════════════════════════════════════════════════════════════════
   Polymarket Bot Dashboard — Client Application
   Real-time monitoring via Server-Sent Events
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    // ── State ──────────────────────────────────────────────────────────────
    let lastState = null;
    let autoScroll = true;
    let eventSource = null;
    let reconnectTimer = null;
    let chartCtx = null;
    let uptimeInterval = null;
    let startTime = null;

    // ── DOM References ────────────────────────────────────────────────────
    const $ = (sel) => document.querySelector(sel);
    const el = {
        modeBadge:      $("#mode-badge"),
        wsBadge:        $("#ws-badge"),
        cycleCount:     $("#cycle-count"),
        sessionPnl:     $("#session-pnl"),
        uptime:         $("#uptime"),
        connectionDot:  $("#connection-dot"),
        totalPnl:       $("#total-pnl"),
        totalTrades:    $("#total-trades"),
        winRate:        $("#win-rate"),
        avgRoi:         $("#avg-roi"),
        marketCountdown:$("#market-countdown"),
        marketInfo:     $("#market-info"),
        yesBid:         $("#yes-bid"),
        yesAsk:         $("#yes-ask"),
        noBid:          $("#no-bid"),
        noAsk:          $("#no-ask"),
        combinedAsk:    $("#combined-ask"),
        spread:         $("#spread"),
        arbStatusBadge: $("#arb-status-badge"),
        arbDetails:     $("#arb-details"),
        priceChart:     $("#price-chart"),
        tradesBody:     $("#trades-body"),
        tradeCount:     $("#trade-count"),
        logContainer:   $("#log-container"),
        logToggle:      $("#log-scroll-toggle"),
    };

    // ── Formatters ────────────────────────────────────────────────────────
    function fmtUsd(v) {
        if (v == null) return "--";
        if (Math.abs(v) < 0.01) return "$" + v.toFixed(4);
        return "$" + v.toFixed(2);
    }

    function fmtPrice(v) {
        if (v == null) return "--";
        return "$" + v.toFixed(2);
    }

    function fmtPct(v) {
        if (v == null) return "--";
        return v.toFixed(2) + "%";
    }

    function fmtCountdown(seconds) {
        if (seconds == null || seconds <= 0) return "EXPIRED";
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
    }

    function fmtUptime(seconds) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);
        return [h, m, s].map((v) => String(v).padStart(2, "0")).join(":");
    }

    function fmtTime(iso) {
        if (!iso) return "--";
        try {
            const d = new Date(iso);
            return d.toLocaleTimeString("en-US", { hour12: false });
        } catch {
            return iso;
        }
    }

    function truncate(str, len) {
        if (!str) return "";
        return str.length > len ? str.slice(0, len) + "..." : str;
    }

    // ── Flash animation helper ────────────────────────────────────────────
    function flashElement(element, direction) {
        const cls = direction === "up" ? "flash-green" : "flash-red";
        element.classList.remove("flash-green", "flash-red");
        // Force reflow
        void element.offsetWidth;
        element.classList.add(cls);
        setTimeout(() => element.classList.remove(cls), 500);
    }

    // ── Update Functions ──────────────────────────────────────────────────

    function updateHeader(s) {
        // Mode badge
        if (s.dry_run) {
            el.modeBadge.textContent = "DRY RUN";
            el.modeBadge.className = "badge badge-dry";
        } else {
            el.modeBadge.textContent = "LIVE";
            el.modeBadge.className = "badge badge-live";
        }

        // WS badge
        if (s.use_websocket && s.ws_connected) {
            el.wsBadge.textContent = "WS ON";
            el.wsBadge.className = "badge badge-on";
        } else if (s.use_websocket) {
            el.wsBadge.textContent = "WS OFF";
            el.wsBadge.className = "badge badge-off";
        } else {
            el.wsBadge.textContent = "WS DISABLED";
            el.wsBadge.className = "badge badge-off";
        }

        el.cycleCount.textContent = s.market_cycles != null ? s.market_cycles : 1;

        // Track start time for local uptime counter
        if (startTime === null && s.uptime_seconds > 0) {
            startTime = Date.now() / 1000 - s.uptime_seconds;
        }
    }

    function updatePnl(s) {
        // Total PnL
        const pnl = s.total_pnl || 0;
        el.totalPnl.textContent = fmtUsd(pnl);
        if (pnl > 0) {
            el.totalPnl.className = "pnl-value mono profit";
        } else if (pnl < 0) {
            el.totalPnl.className = "pnl-value mono loss";
        } else {
            el.totalPnl.className = "pnl-value mono neutral";
        }

        // Session PnL
        const spnl = s.session_pnl || 0;
        el.sessionPnl.textContent = fmtUsd(spnl);
        if (spnl > 0) {
            el.sessionPnl.className = "pnl-value mono profit";
        } else if (spnl < 0) {
            el.sessionPnl.className = "pnl-value mono loss";
        } else {
            el.sessionPnl.className = "pnl-value mono neutral";
        }

        el.totalTrades.textContent = s.total_trades || 0;

        // Win rate
        if (s.total_trades > 0) {
            const wr = ((s.winning_trades || 0) / s.total_trades * 100);
            el.winRate.textContent = fmtPct(wr);
        } else {
            el.winRate.textContent = "--";
        }

        // Average ROI from trades
        if (s.trades && s.trades.length > 0) {
            const sum = s.trades.reduce((acc, t) => acc + (t.roi_pct || 0), 0);
            el.avgRoi.textContent = fmtPct(sum / s.trades.length);
        } else {
            el.avgRoi.textContent = "--";
        }
    }

    function updateMarket(s) {
        const m = s.market;
        if (!m) {
            el.marketInfo.innerHTML = '<div class="market-empty">Waiting for market data...</div>';
            el.marketCountdown.textContent = "";
            return;
        }

        // Countdown
        const remaining = m.time_remaining;
        if (remaining != null && remaining > 0) {
            el.marketCountdown.textContent = fmtCountdown(remaining);
            if (remaining > 300) {
                el.marketCountdown.className = "countdown mono safe";
            } else if (remaining > 60) {
                el.marketCountdown.className = "countdown mono warning";
            } else {
                el.marketCountdown.className = "countdown mono urgent";
            }
        } else if (remaining != null) {
            el.marketCountdown.textContent = "EXPIRED";
            el.marketCountdown.className = "countdown mono urgent";
        } else {
            el.marketCountdown.textContent = "";
        }

        const restRemaining = s.rest_remaining || 0;
        const restBanner = restRemaining > 0
            ? `<div class="market-rest-banner">\u23F8 RESTING \u2014 ${fmtCountdown(restRemaining)} remaining</div>`
            : "";

        el.marketInfo.innerHTML = `
            <div class="market-question">${escHtml(m.question)}</div>
            ${restBanner}
            <div class="market-meta">
                <div class="market-meta-row">
                    <span class="market-meta-label">Condition ID</span>
                    <span class="market-meta-value">${truncate(m.condition_id, 24)}</span>
                </div>
                <div class="market-meta-row">
                    <span class="market-meta-label">Fee Rate</span>
                    <span class="market-meta-value">${m.fee_rate_bps} bps</span>
                </div>
                <div class="market-meta-row">
                    <span class="market-meta-label">Status</span>
                    <span class="market-meta-value">${m.active ? "Active" : "Inactive"}</span>
                </div>
            </div>
        `;
    }

    function updatePrices(s) {
        const p = s.prices;
        if (!p) return;

        const prev = lastState?.prices;

        setPrice(el.yesBid, p.yes_bid, prev?.yes_bid);
        setPrice(el.yesAsk, p.yes_ask, prev?.yes_ask);
        setPrice(el.noBid, p.no_bid, prev?.no_bid);
        setPrice(el.noAsk, p.no_ask, prev?.no_ask);

        el.combinedAsk.textContent = p.combined_ask != null ? fmtUsd(p.combined_ask) : "--";

        if (p.combined_ask != null) {
            const spread = 1.0 - p.combined_ask;
            el.spread.textContent = fmtUsd(spread);
            el.spread.style.color = spread > 0
                ? "var(--green)"
                : spread < 0 ? "var(--red)" : "var(--text-secondary)";
        } else {
            el.spread.textContent = "--";
            el.spread.style.color = "";
        }
    }

    function setPrice(element, val, prevVal) {
        element.textContent = fmtPrice(val);
        if (prevVal != null && val != null && val !== prevVal) {
            flashElement(element, val > prevVal ? "up" : "down");
        }
    }

    function updateArb(s) {
        const a = s.arb;
        if (!a) {
            el.arbStatusBadge.textContent = "NO SIGNAL";
            el.arbStatusBadge.className = "badge badge-none";
            el.arbDetails.innerHTML = '<div class="arb-empty">Awaiting data...</div>';
            return;
        }

        if (a.is_profitable) {
            el.arbStatusBadge.textContent = "PROFITABLE";
            el.arbStatusBadge.className = "badge badge-profitable";

            el.arbDetails.innerHTML = `
                <div class="arb-profitable">
                    <div class="arb-profitable-text">Arbitrage opportunity detected</div>
                </div>
                <div class="arb-grid">
                    <div class="arb-item">
                        <span class="arb-item-label">Net Profit</span>
                        <span class="arb-item-value" style="color: var(--green)">${fmtUsd(a.net_profit)}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">ROI</span>
                        <span class="arb-item-value" style="color: var(--green)">${fmtPct(a.roi_pct)}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">Total Fees</span>
                        <span class="arb-item-value">${fmtUsd(a.total_fees)}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">Max Size</span>
                        <span class="arb-item-value">${a.max_profitable_size ? a.max_profitable_size.toFixed(0) + " shares" : "--"}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">YES Liq.</span>
                        <span class="arb-item-value">${a.yes_liquidity ? a.yes_liquidity.toFixed(0) : "--"}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">NO Liq.</span>
                        <span class="arb-item-value">${a.no_liquidity ? a.no_liquidity.toFixed(0) : "--"}</span>
                    </div>
                </div>
            `;
        } else {
            el.arbStatusBadge.textContent = "NO ARB";
            el.arbStatusBadge.className = "badge badge-none";

            el.arbDetails.innerHTML = `
                <div class="arb-grid">
                    <div class="arb-item">
                        <span class="arb-item-label">Combined</span>
                        <span class="arb-item-value">${fmtUsd(a.combined_cost)}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">Spread</span>
                        <span class="arb-item-value">${fmtUsd(a.gross_spread)}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">Fees</span>
                        <span class="arb-item-value">${fmtUsd(a.total_fees)}</span>
                    </div>
                    <div class="arb-item">
                        <span class="arb-item-label">Net</span>
                        <span class="arb-item-value" style="color: var(--red)">${fmtUsd(a.net_profit)}</span>
                    </div>
                </div>
            `;
        }
    }

    function updateTrades(s) {
        const trades = s.trades || [];
        el.tradeCount.textContent = `${trades.length} trade${trades.length !== 1 ? "s" : ""}`;

        if (trades.length === 0) {
            el.tradesBody.innerHTML = '<tr class="empty-row"><td colspan="9">No trades yet</td></tr>';
            return;
        }

        el.tradesBody.innerHTML = trades.map((t) => {
            const statusCls = t.status === "SUCCESS" ? "status-success"
                : t.status === "PARTIAL" ? "status-partial"
                : t.status === "RESOLVED" ? "status-success"
                : t.status === "WON" ? "status-success"
                : t.status === "LOST" ? "status-failed"
                : "status-failed";
            const tradeType = t.trade_type || "arb";
            const typeLabel = tradeType === "buy_yes" ? "BUY YES"
                : tradeType === "buy_no" ? "BUY NO" : "ARB";
            const typeCls = tradeType === "buy_yes" ? "style=\"color: var(--green)\""
                : tradeType === "buy_no" ? "style=\"color: var(--red)\""
                : "style=\"color: var(--purple)\"";
            const cost = t.cost || (t.size * ((t.yes_price || 0) + (t.no_price || 0)));
            let profitStr;
            if (t.resolved) {
                // Market ended — show final realized PnL
                const pnl = t.net_profit || 0;
                profitStr = `<span style="color: ${pnl >= 0 ? "var(--green)" : "var(--red)"}">${fmtUsd(pnl)}</span>`;
            } else if (t.unrealized_pnl !== undefined && t.unrealized_pnl !== null && t.unrealized_pnl !== 0) {
                // Live unrealized PnL
                const pnl = t.unrealized_pnl;
                profitStr = `<span style="color: ${pnl >= 0 ? "var(--green)" : "var(--red)"}">${fmtUsd(pnl)}</span>`;
            } else if (tradeType === "arb") {
                const pnl = t.net_profit || 0;
                profitStr = `<span style="color: ${pnl > 0 ? "var(--green)" : "var(--red)"}">${fmtUsd(pnl)}</span>`;
            } else {
                profitStr = `<span style="color: var(--text-muted)">--</span>`;
            }
            return `<tr>
                <td>${fmtTime(t.timestamp)}</td>
                <td ${typeCls}>${typeLabel}</td>
                <td>${truncate(t.market_question, 24)}</td>
                <td>${t.size.toFixed(0)}</td>
                <td>${fmtUsd(cost)}</td>
                <td>${t.yes_price ? fmtPrice(t.yes_price) : "--"}</td>
                <td>${t.no_price ? fmtPrice(t.no_price) : "--"}</td>
                <td>${profitStr}</td>
                <td class="${statusCls}">${t.status}${t.dry_run ? " (dry)" : ""}</td>
            </tr>`;
        }).join("");
    }

    function updateLogs(s) {
        const logs = s.logs || [];
        if (logs.length === 0) return;

        const container = el.logContainer;
        const existingCount = container.querySelectorAll(".log-line").length;

        // Only append new lines
        if (existingCount === 0) {
            container.innerHTML = "";
        }

        const newLines = logs.slice(existingCount);
        for (const line of newLines) {
            const div = document.createElement("div");
            div.className = "log-line " + logLevel(line);
            div.textContent = line;
            container.appendChild(div);
        }

        // Trim if too many
        while (container.children.length > 200) {
            container.removeChild(container.firstChild);
        }

        if (autoScroll) {
            container.scrollTop = container.scrollHeight;
        }
    }

    function logLevel(line) {
        if (line.includes("ERROR"))   return "log-error";
        if (line.includes("WARNING")) return "log-warning";
        if (line.includes("DEBUG"))   return "log-debug";
        return "log-info";
    }

    // ── Chart ─────────────────────────────────────────────────────────────

    function initChart() {
        const canvas = el.priceChart;
        if (!canvas) return;
        chartCtx = canvas.getContext("2d");
    }

    function drawChart(s) {
        if (!chartCtx) return;
        const history = s.price_history || [];
        if (history.length < 2) return;

        const canvas = chartCtx.canvas;
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        chartCtx.scale(dpr, dpr);

        const W = rect.width;
        const H = rect.height;
        const pad = { top: 12, right: 12, bottom: 24, left: 50 };
        const plotW = W - pad.left - pad.right;
        const plotH = H - pad.top - pad.bottom;

        chartCtx.clearRect(0, 0, W, H);

        // Extract data
        const yesData = history.map((p) => p.yes_ask).filter((v) => v != null);
        const noData  = history.map((p) => p.no_ask).filter((v) => v != null);
        const combData = history.map((p) => p.combined_ask).filter((v) => v != null);

        if (yesData.length < 2) return;

        const allVals = [...yesData, ...noData, ...combData];
        let minV = Math.min(...allVals);
        let maxV = Math.max(...allVals);
        const range = maxV - minV;
        // Add 10% padding
        minV -= range * 0.1 || 0.01;
        maxV += range * 0.1 || 0.01;

        const toX = (i, len) => pad.left + (i / (len - 1)) * plotW;
        const toY = (v) => pad.top + (1 - (v - minV) / (maxV - minV)) * plotH;

        // Grid lines
        chartCtx.strokeStyle = "rgba(42, 42, 53, 0.6)";
        chartCtx.lineWidth = 1;
        const gridLines = 4;
        for (let i = 0; i <= gridLines; i++) {
            const y = pad.top + (i / gridLines) * plotH;
            chartCtx.beginPath();
            chartCtx.moveTo(pad.left, y);
            chartCtx.lineTo(W - pad.right, y);
            chartCtx.stroke();

            // Label
            const val = maxV - (i / gridLines) * (maxV - minV);
            chartCtx.fillStyle = "rgba(136, 136, 160, 0.6)";
            chartCtx.font = "10px 'JetBrains Mono', monospace";
            chartCtx.textAlign = "right";
            chartCtx.fillText("$" + val.toFixed(2), pad.left - 6, y + 3);
        }

        // Draw series
        function drawLine(data, fullHistory, color, width) {
            // Map data accounting for nulls in the full history
            const points = [];
            let di = 0;
            for (let i = 0; i < fullHistory.length; i++) {
                const key = color === getComputedStyle(document.documentElement).getPropertyValue("--chart-yes").trim() ? "yes_ask"
                    : color === getComputedStyle(document.documentElement).getPropertyValue("--chart-no").trim() ? "no_ask"
                    : "combined_ask";
                const v = fullHistory[i][key];
                if (v != null) {
                    points.push({ x: toX(i, fullHistory.length), y: toY(v) });
                }
            }

            if (points.length < 2) return;

            chartCtx.strokeStyle = color;
            chartCtx.lineWidth = width;
            chartCtx.lineJoin = "round";
            chartCtx.lineCap = "round";
            chartCtx.beginPath();
            chartCtx.moveTo(points[0].x, points[0].y);
            for (let i = 1; i < points.length; i++) {
                chartCtx.lineTo(points[i].x, points[i].y);
            }
            chartCtx.stroke();
        }

        // Simpler approach: draw from filtered arrays
        function drawSeries(values, color, width) {
            if (values.length < 2) return;
            chartCtx.strokeStyle = color;
            chartCtx.lineWidth = width;
            chartCtx.lineJoin = "round";
            chartCtx.lineCap = "round";
            chartCtx.beginPath();
            for (let i = 0; i < values.length; i++) {
                const x = toX(i, values.length);
                const y = toY(values[i]);
                if (i === 0) chartCtx.moveTo(x, y);
                else chartCtx.lineTo(x, y);
            }
            chartCtx.stroke();
        }

        drawSeries(combData, "#a855f7", 2);
        drawSeries(yesData, "#22c55e", 1.5);
        drawSeries(noData, "#ef4444", 1.5);

        // $1.00 reference line
        if (minV < 1.0 && maxV > 1.0) {
            chartCtx.setLineDash([4, 4]);
            chartCtx.strokeStyle = "rgba(136, 136, 160, 0.3)";
            chartCtx.lineWidth = 1;
            chartCtx.beginPath();
            const y1 = toY(1.0);
            chartCtx.moveTo(pad.left, y1);
            chartCtx.lineTo(W - pad.right, y1);
            chartCtx.stroke();
            chartCtx.setLineDash([]);

            chartCtx.fillStyle = "rgba(136, 136, 160, 0.5)";
            chartCtx.font = "10px 'JetBrains Mono', monospace";
            chartCtx.textAlign = "left";
            chartCtx.fillText("$1.00", W - pad.right + 4, y1 + 3);
        }
    }

    // ── SSE Connection ────────────────────────────────────────────────────

    function connect() {
        if (eventSource) {
            eventSource.close();
        }

        eventSource = new EventSource("/api/stream");

        eventSource.onopen = () => {
            el.connectionDot.className = "connection-dot connected";
            el.connectionDot.title = "Connected";
        };

        eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                applyState(data);
            } catch (e) {
                console.error("Failed to parse SSE data:", e);
            }
        };

        eventSource.onerror = () => {
            el.connectionDot.className = "connection-dot disconnected";
            el.connectionDot.title = "Disconnected — reconnecting...";
            eventSource.close();
            // Reconnect after delay
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 3000);
        };
    }

    function applyState(s) {
        updateHeader(s);
        updatePnl(s);
        updateMarket(s);
        updatePrices(s);
        updateArb(s);
        updateTrades(s);
        updateLogs(s);
        drawChart(s);

        lastState = s;
    }

    // ── Uptime Counter ────────────────────────────────────────────────────

    function startUptimeCounter() {
        uptimeInterval = setInterval(() => {
            if (startTime != null) {
                const elapsed = Date.now() / 1000 - startTime;
                el.uptime.textContent = fmtUptime(elapsed);
            }
        }, 1000);
    }

    // ── Log scroll toggle ─────────────────────────────────────────────────

    function initLogToggle() {
        el.logToggle.classList.add("active");
        el.logToggle.addEventListener("click", () => {
            autoScroll = !autoScroll;
            el.logToggle.classList.toggle("active", autoScroll);
        });
    }

    // ── HTML escaping ─────────────────────────────────────────────────────

    function escHtml(str) {
        if (!str) return "";
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // ── Settings Panel ────────────────────────────────────────────────────

    const SETTINGS_DEFAULTS = {
        MAX_POSITION_SIZE: 100,
        MAX_BUDGET: 1000,
        MAX_CONCURRENT_POSITIONS: 3,
        ARB_MIN_PROFIT: 0.005,
        ARB_MIN_ROI_PCT: 0.3,
        MAX_LOSS_PER_TRADE: 10,
        MAX_DAILY_LOSS: 50,
        STOP_LOSS_ENABLED: false,
        STOP_LOSS_AMOUNT: 100,
        DRY_RUN: true,
        AUTO_EXECUTE: false,
        ARB_COOLDOWN_SECONDS: 120,
        POLLING_INTERVAL: 1,
        USE_WEBSOCKET: true,
        BUY_YES_TRIGGER: 0,
        BUY_NO_TRIGGER: 0,
        DIRECTIONAL_BUY_SIZE: 50,
        MARKET_REST_SECONDS: 0,
    };

    const BOOL_FIELDS = new Set([
        "DRY_RUN", "AUTO_EXECUTE", "USE_WEBSOCKET", "STOP_LOSS_ENABLED",
    ]);

    const INT_FIELDS = new Set([
        "MAX_CONCURRENT_POSITIONS", "ARB_COOLDOWN_SECONDS", "POLLING_INTERVAL",
        "MARKET_REST_SECONDS",
    ]);

    const settingsDrawer = $("#settings-drawer");
    const settingsOverlay = $("#settings-overlay");
    const settingsForm = $("#settings-form");
    const toastEl = $("#toast");

    function openSettings() {
        // Fetch current values before showing
        fetchSettings().then(() => {
            settingsDrawer.classList.add("open");
            settingsOverlay.classList.add("open");
        });
    }

    function closeSettings() {
        settingsDrawer.classList.remove("open");
        settingsOverlay.classList.remove("open");
    }

    function populateForm(settings) {
        for (const [key, value] of Object.entries(settings)) {
            const input = settingsForm.querySelector(`[name="${key}"]`);
            if (!input) continue;
            if (input.type === "checkbox") {
                input.checked = !!value;
            } else {
                input.value = value;
            }
        }
        updateStopLossVisibility();
    }

    function gatherFormData() {
        const data = {};
        for (const key of Object.keys(SETTINGS_DEFAULTS)) {
            const input = settingsForm.querySelector(`[name="${key}"]`);
            if (!input) continue;
            if (BOOL_FIELDS.has(key)) {
                data[key] = input.checked;
            } else if (INT_FIELDS.has(key)) {
                data[key] = parseInt(input.value, 10);
            } else {
                data[key] = parseFloat(input.value);
            }
        }
        return data;
    }

    async function fetchSettings() {
        try {
            const res = await fetch("/api/settings");
            if (res.ok) {
                const settings = await res.json();
                populateForm(settings);
            }
        } catch (e) {
            console.error("Failed to fetch settings:", e);
        }
    }

    async function saveSettings() {
        const btn = $("#settings-save");
        btn.disabled = true;
        btn.textContent = "Saving...";
        try {
            const data = gatherFormData();
            const res = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(data),
            });
            const result = await res.json();
            if (result.ok) {
                showToast("Settings saved", "success");
                closeSettings();
            } else {
                showToast(result.errors?.[0] || "Failed to save", "error");
            }
        } catch (e) {
            showToast("Network error", "error");
        } finally {
            btn.disabled = false;
            btn.textContent = "Save Settings";
        }
    }

    function resetSettings() {
        populateForm(SETTINGS_DEFAULTS);
    }

    function updateStopLossVisibility() {
        const enabled = settingsForm.querySelector('[name="STOP_LOSS_ENABLED"]');
        const field = $("#stop-loss-amount-field");
        if (enabled && field) {
            field.style.opacity = enabled.checked ? "1" : "0.4";
            field.querySelector("input").disabled = !enabled.checked;
        }
    }

    let toastTimer = null;

    function showToast(message, type) {
        clearTimeout(toastTimer);
        toastEl.textContent = message;
        toastEl.className = "toast toast-" + type + " show";
        toastTimer = setTimeout(() => {
            toastEl.classList.remove("show");
        }, 3000);
    }

    function initSettings() {
        $("#settings-btn").addEventListener("click", openSettings);
        $("#settings-close").addEventListener("click", closeSettings);
        settingsOverlay.addEventListener("click", closeSettings);
        $("#settings-save").addEventListener("click", saveSettings);
        $("#settings-reset").addEventListener("click", resetSettings);

        // Close on Escape
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && settingsDrawer.classList.contains("open")) {
                closeSettings();
            }
        });

        // Toggle stop-loss amount visibility
        const stopLossToggle = settingsForm.querySelector('[name="STOP_LOSS_ENABLED"]');
        if (stopLossToggle) {
            stopLossToggle.addEventListener("change", updateStopLossVisibility);
        }
    }

    // ── Uninstall ────────────────────────────────────────────────────────

    const uninstallOverlay = $("#uninstall-overlay");

    function openUninstallModal() {
        uninstallOverlay.classList.add("open");
    }

    function closeUninstallModal() {
        uninstallOverlay.classList.remove("open");
        // Reset state in case it was in loading/done state
        const confirmBtn = $("#uninstall-confirm");
        confirmBtn.classList.remove("loading");
        confirmBtn.disabled = false;
    }

    async function performUninstall() {
        const confirmBtn = $("#uninstall-confirm");
        confirmBtn.classList.add("loading");
        confirmBtn.disabled = true;
        $("#uninstall-cancel").disabled = true;

        try {
            const res = await fetch("/api/uninstall", { method: "POST" });
            const result = await res.json();

            if (res.ok && result.ok) {
                // Show final message
                const modal = $("#uninstall-modal");
                modal.innerHTML = `
                    <div class="uninstall-done">
                        <div class="uninstall-modal-title" style="margin-bottom: 12px;">Uninstalled</div>
                        <div class="uninstall-done-text">PolymarketBot has been uninstalled. You can close this window.</div>
                    </div>
                `;
            } else {
                const errMsg = result.error || result.message || "Uninstall failed";
                showToast(errMsg, "error");
                confirmBtn.classList.remove("loading");
                confirmBtn.disabled = false;
                $("#uninstall-cancel").disabled = false;
            }
        } catch (e) {
            showToast("Network error during uninstall", "error");
            confirmBtn.classList.remove("loading");
            confirmBtn.disabled = false;
            $("#uninstall-cancel").disabled = false;
        }
    }

    function initUninstall() {
        const uninstallBtn = $("#uninstall-btn");
        if (uninstallBtn) {
            uninstallBtn.addEventListener("click", openUninstallModal);
        }

        const cancelBtn = $("#uninstall-cancel");
        if (cancelBtn) {
            cancelBtn.addEventListener("click", closeUninstallModal);
        }

        const confirmBtn = $("#uninstall-confirm");
        if (confirmBtn) {
            confirmBtn.addEventListener("click", performUninstall);
        }

        // Close on overlay click (outside modal)
        if (uninstallOverlay) {
            uninstallOverlay.addEventListener("click", (e) => {
                if (e.target === uninstallOverlay) closeUninstallModal();
            });
        }

        // Close on Escape
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && uninstallOverlay.classList.contains("open")) {
                closeUninstallModal();
            }
        });
    }

    // ── Update Check ─────────────────────────────────────────────────────

    let updatePollTimer = null;

    function initUpdateCheck() {
        // Initial check after 3-second delay
        setTimeout(checkForUpdate, 3000);
        // Poll every 60s until banner is shown
        updatePollTimer = setInterval(checkForUpdate, 60000);
    }

    async function checkForUpdate() {
        try {
            const res = await fetch("/api/update-status");
            if (!res.ok) return;
            const data = await res.json();

            // Always update current version in header if available
            if (data.current_version) {
                const versionEl = $("#app-version");
                if (versionEl && !versionEl.textContent) {
                    versionEl.textContent = "v" + data.current_version;
                }
            }

            if (data.error || !data.available) return;

            // Check if this version was previously dismissed
            const dismissed = localStorage.getItem("dismissed_update_version");
            if (dismissed === data.latest_version) return;

            showUpdateBanner(data.latest_version, data.download_url);

            // Stop polling once banner is shown
            if (updatePollTimer) {
                clearInterval(updatePollTimer);
                updatePollTimer = null;
            }
        } catch (e) {
            // Silently ignore — update check is non-critical
        }
    }

    function showUpdateBanner(version, url) {
        const banner = $("#update-banner");
        if (!banner) return;

        $("#update-version").textContent = "v" + version;
        $("#update-download").href = url;

        banner.hidden = false;

        // Dismiss handler
        $("#update-dismiss").addEventListener("click", () => {
            banner.hidden = true;
            localStorage.setItem("dismissed_update_version", version);
        });
    }

    // ── Init ──────────────────────────────────────────────────────────────

    function init() {
        initChart();
        initLogToggle();
        initSettings();
        initUninstall();
        initUpdateCheck();
        startUptimeCounter();
        connect();

        // Handle chart resize
        window.addEventListener("resize", () => {
            if (lastState) drawChart(lastState);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();

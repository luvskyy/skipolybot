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
    let currentChannel = "stable";
    let betaWarningSuppressed = false;

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
        btcPrice:       $("#btc-price"),
        btcIndicator:   $("#btc-price-indicator"),
        strikePrice:    $("#strike-price"),
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

    function fmtBtc(v) {
        if (v == null) return "--";
        return "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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

    function updateBtcPrice(s) {
        const btcPrice = s.btc_price;
        const strikePrice = s.market?.strike_price;

        // BTC Price
        if (btcPrice != null) {
            const prevBtc = lastState?.btc_price;
            el.btcPrice.textContent = fmtBtc(btcPrice);

            if (strikePrice != null) {
                const isAbove = btcPrice >= strikePrice;
                el.btcPrice.className = "btc-price-value mono " + (isAbove ? "above" : "below");
                const diff = btcPrice - strikePrice;
                const sign = diff >= 0 ? "+" : "";
                el.btcIndicator.textContent = (isAbove ? "ABOVE" : "BELOW") + " " + sign + fmtBtc(diff).replace("$", "$");
                el.btcIndicator.className = "btc-price-indicator " + (isAbove ? "above" : "below");
            } else {
                el.btcPrice.className = "btc-price-value mono";
                el.btcIndicator.textContent = "";
                el.btcIndicator.className = "btc-price-indicator";
            }

            if (prevBtc != null && btcPrice !== prevBtc) {
                flashElement(el.btcPrice, btcPrice > prevBtc ? "up" : "down");
            }
        } else {
            el.btcPrice.textContent = "--";
            el.btcPrice.className = "btc-price-value mono";
            el.btcIndicator.textContent = "";
            el.btcIndicator.className = "btc-price-indicator";
        }

        // Strike Price
        if (strikePrice != null) {
            el.strikePrice.textContent = fmtBtc(strikePrice);
        } else {
            el.strikePrice.textContent = "--";
        }
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
                : t.status === "STOPPED" ? "status-stopped"
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
            const tid = t.trade_id != null ? t.trade_id : "";
            return `<tr data-trade-id="${tid}">
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

        // Attach click handlers for trade detail
        el.tradesBody.querySelectorAll("tr[data-trade-id]").forEach(function (row) {
            row.addEventListener("click", function () {
                var tradeId = this.getAttribute("data-trade-id");
                if (tradeId !== "") openTradeDetail(tradeId);
            });
        });
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
        const btcHistory = s.btc_price_history || [];
        const strikePrice = s.market?.strike_price;

        if (btcHistory.length < 2) return;

        const canvas = chartCtx.canvas;
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        chartCtx.scale(dpr, dpr);

        const W = rect.width;
        const H = rect.height;
        const pad = { top: 16, right: 60, bottom: 24, left: 70 };
        const plotW = W - pad.left - pad.right;
        const plotH = H - pad.top - pad.bottom;

        chartCtx.clearRect(0, 0, W, H);

        // Extract BTC price data
        const btcData = btcHistory.map((p) => p.price).filter((v) => v != null);
        if (btcData.length < 2) return;

        // Compute range including strike price
        const allVals = [...btcData];
        if (strikePrice != null) allVals.push(strikePrice);
        let minV = Math.min(...allVals);
        let maxV = Math.max(...allVals);
        const range = maxV - minV;
        const padding = Math.max(range * 0.15, 50); // at least $50 padding
        minV -= padding;
        maxV += padding;

        const toX = (i) => pad.left + (i / (btcData.length - 1)) * plotW;
        const toY = (v) => pad.top + (1 - (v - minV) / (maxV - minV)) * plotH;

        // Grid lines
        chartCtx.strokeStyle = "rgba(42, 42, 53, 0.5)";
        chartCtx.lineWidth = 1;
        const gridLines = 4;
        for (let i = 0; i <= gridLines; i++) {
            const y = pad.top + (i / gridLines) * plotH;
            chartCtx.beginPath();
            chartCtx.moveTo(pad.left, y);
            chartCtx.lineTo(W - pad.right, y);
            chartCtx.stroke();

            const val = maxV - (i / gridLines) * (maxV - minV);
            chartCtx.fillStyle = "rgba(136, 136, 160, 0.5)";
            chartCtx.font = "10px 'JetBrains Mono', monospace";
            chartCtx.textAlign = "right";
            chartCtx.fillText("$" + val.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 }), pad.left - 8, y + 3);
        }

        // Strike price reference line
        if (strikePrice != null && strikePrice >= minV && strikePrice <= maxV) {
            const sy = toY(strikePrice);

            // Dashed line
            chartCtx.setLineDash([6, 4]);
            chartCtx.strokeStyle = "rgba(234, 179, 8, 0.5)";
            chartCtx.lineWidth = 1.5;
            chartCtx.beginPath();
            chartCtx.moveTo(pad.left, sy);
            chartCtx.lineTo(W - pad.right, sy);
            chartCtx.stroke();
            chartCtx.setLineDash([]);

            // Label on right
            chartCtx.fillStyle = "rgba(234, 179, 8, 0.8)";
            chartCtx.font = "10px 'JetBrains Mono', monospace";
            chartCtx.textAlign = "left";
            chartCtx.fillText("$" + strikePrice.toLocaleString("en-US"), W - pad.right + 6, sy + 3);
        }

        // BTC price line with gradient fill
        // First draw the filled area under/over the strike
        if (strikePrice != null && btcData.length > 1) {
            const sy = toY(strikePrice);

            // Green fill above strike
            chartCtx.beginPath();
            chartCtx.moveTo(toX(0), Math.min(toY(btcData[0]), sy));
            for (let i = 0; i < btcData.length; i++) {
                const x = toX(i);
                const y = toY(btcData[i]);
                chartCtx.lineTo(x, Math.min(y, sy));
            }
            chartCtx.lineTo(toX(btcData.length - 1), sy);
            chartCtx.lineTo(toX(0), sy);
            chartCtx.closePath();
            const greenGrad = chartCtx.createLinearGradient(0, pad.top, 0, sy);
            greenGrad.addColorStop(0, "rgba(34, 197, 94, 0.12)");
            greenGrad.addColorStop(1, "rgba(34, 197, 94, 0.02)");
            chartCtx.fillStyle = greenGrad;
            chartCtx.fill();

            // Red fill below strike
            chartCtx.beginPath();
            chartCtx.moveTo(toX(0), Math.max(toY(btcData[0]), sy));
            for (let i = 0; i < btcData.length; i++) {
                const x = toX(i);
                const y = toY(btcData[i]);
                chartCtx.lineTo(x, Math.max(y, sy));
            }
            chartCtx.lineTo(toX(btcData.length - 1), sy);
            chartCtx.lineTo(toX(0), sy);
            chartCtx.closePath();
            const redGrad = chartCtx.createLinearGradient(0, sy, 0, pad.top + plotH);
            redGrad.addColorStop(0, "rgba(239, 68, 68, 0.02)");
            redGrad.addColorStop(1, "rgba(239, 68, 68, 0.12)");
            chartCtx.fillStyle = redGrad;
            chartCtx.fill();
        }

        // Draw BTC price line
        chartCtx.strokeStyle = "#3b82f6";
        chartCtx.lineWidth = 2;
        chartCtx.lineJoin = "round";
        chartCtx.lineCap = "round";
        chartCtx.beginPath();
        for (let i = 0; i < btcData.length; i++) {
            const x = toX(i);
            const y = toY(btcData[i]);
            if (i === 0) chartCtx.moveTo(x, y);
            else chartCtx.lineTo(x, y);
        }
        chartCtx.stroke();

        // Current price dot (last point)
        const lastIdx = btcData.length - 1;
        const lastX = toX(lastIdx);
        const lastY = toY(btcData[lastIdx]);
        const isAbove = strikePrice != null ? btcData[lastIdx] >= strikePrice : true;
        const dotColor = isAbove ? "#22c55e" : "#ef4444";

        // Glow
        chartCtx.beginPath();
        chartCtx.arc(lastX, lastY, 6, 0, Math.PI * 2);
        chartCtx.fillStyle = (isAbove ? "rgba(34, 197, 94, 0.2)" : "rgba(239, 68, 68, 0.2)");
        chartCtx.fill();

        // Dot
        chartCtx.beginPath();
        chartCtx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        chartCtx.fillStyle = dotColor;
        chartCtx.fill();

        // Current price label
        chartCtx.fillStyle = dotColor;
        chartCtx.font = "bold 10px 'JetBrains Mono', monospace";
        chartCtx.textAlign = "left";
        chartCtx.fillText(
            "$" + btcData[lastIdx].toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
            W - pad.right + 6,
            lastY + 3
        );
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
        updateBtcPrice(s);
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

    // ── Trade Detail Panel ─────────────────────────────────────────────────

    const tradeDetailDrawer  = $("#trade-detail-drawer");
    const tradeDetailOverlay = $("#trade-detail-overlay");
    const tradeDetailBody    = $("#trade-detail-body");
    let tradeChartCtx        = null;

    function openTradeDetail(tradeId) {
        // Show drawer immediately with loading state
        tradeDetailBody.innerHTML = '<div class="td-loading"><div class="td-loading-spinner"></div>Loading trade details...</div>';
        tradeDetailDrawer.classList.add("open");
        tradeDetailOverlay.classList.add("open");

        // Reset header to generic while loading
        $("#td-type-badge").textContent = "...";
        $("#td-type-badge").className = "trade-type-badge type-arb";
        $("#td-time").textContent = "";

        // Fetch trade detail
        fetch("/api/trade/" + tradeId)
            .then(function (res) {
                if (!res.ok) throw new Error("HTTP " + res.status);
                return res.json();
            })
            .then(function (trade) {
                renderTradeDetail(trade);
            })
            .catch(function (err) {
                tradeDetailBody.innerHTML = '<div class="td-error">Failed to load trade details</div>';
                console.error("Trade detail fetch error:", err);
            });
    }

    function closeTradeDetail() {
        tradeDetailDrawer.classList.remove("open");
        tradeDetailOverlay.classList.remove("open");
    }

    function renderTradeDetail(t) {
        // Header badge
        var typeBadge = $("#td-type-badge");
        var tradeType = t.trade_type || "arb";
        if (tradeType === "buy_yes") {
            typeBadge.textContent = "BUY YES";
            typeBadge.className = "trade-type-badge type-buy-yes";
        } else if (tradeType === "buy_no") {
            typeBadge.textContent = "BUY NO";
            typeBadge.className = "trade-type-badge type-buy-no";
        } else {
            typeBadge.textContent = "ARB";
            typeBadge.className = "trade-type-badge type-arb";
        }

        // Header time
        $("#td-time").textContent = fmtTime(t.timestamp);

        // Build body HTML
        var html = "";

        // Market question
        html += '<div class="td-market-question">';
        html += '<span class="td-mq-label">Market</span>';
        html += escHtml(t.market_question || "--");
        html += "</div>";

        // Summary cards
        var pnlVal = t.resolved ? (t.net_profit || 0) : (t.unrealized_pnl || t.net_profit || 0);
        var pnlCls = pnlVal > 0 ? "profit" : pnlVal < 0 ? "loss" : "";
        var roiVal = t.roi_pct != null ? fmtPct(t.roi_pct) : "--";

        var statusLabel = t.status || "UNKNOWN";
        var statusCls = "badge-failed";
        if (statusLabel === "SUCCESS" || statusLabel === "RESOLVED" || statusLabel === "WON") statusCls = "badge-success";
        else if (statusLabel === "PARTIAL") statusCls = "badge-partial";
        else if (statusLabel === "LOST") statusCls = "badge-failed";

        html += '<div class="td-summary">';
        html += '<div class="td-summary-card"><span class="td-summary-label">Cost</span><span class="td-summary-value mono">' + fmtUsd(t.cost) + "</span></div>";
        html += '<div class="td-summary-card"><span class="td-summary-label">Size</span><span class="td-summary-value mono">' + (t.size != null ? t.size.toFixed(0) + " shares" : "--") + "</span></div>";
        html += '<div class="td-summary-card"><span class="td-summary-label">Entry Price</span><span class="td-summary-value mono">' + fmtPrice(t.entry_price || t.yes_price) + "</span></div>";
        html += '<div class="td-summary-card"><span class="td-summary-label">P&L</span><span class="td-summary-value mono ' + pnlCls + '">' + fmtUsd(pnlVal) + "</span></div>";
        html += '<div class="td-summary-card"><span class="td-summary-label">ROI</span><span class="td-summary-value mono ' + pnlCls + '">' + roiVal + "</span></div>";
        html += '<div class="td-summary-card"><span class="td-summary-label">Status</span><span class="td-status-badge ' + statusCls + '">' + statusLabel + "</span>" + (t.dry_run ? '<span class="td-dry-tag">DRY</span>' : "") + "</div>";
        html += "</div>";

        // Entry context section
        html += '<div class="td-section">';
        html += '<div class="td-section-title">At Entry</div>';
        html += '<div class="td-grid">';
        html += tdGridItem("Time Remaining", t.time_remaining != null ? fmtCountdown(t.time_remaining) : "--");
        html += tdGridItem("Market Age", t.market_age != null ? fmtCountdown(t.market_age) : "--");
        html += tdGridItem("Combined Ask", fmtUsd(t.combined_ask_at_entry));
        html += tdGridItem("Spread", fmtUsd(t.gross_spread));
        html += tdGridItem("Fee Rate", t.fee_rate_bps != null ? t.fee_rate_bps + " bps" : "--");
        html += tdGridItem("YES Bid", fmtPrice(t.yes_bid_at_entry));
        html += tdGridItem("YES Ask", fmtPrice(t.yes_price));
        html += tdGridItem("NO Bid", fmtPrice(t.no_bid_at_entry));
        html += tdGridItem("NO Ask", fmtPrice(t.no_price));

        if (tradeType === "arb") {
            html += tdGridItem("Max Arb Size", t.arb_max_size != null ? t.arb_max_size.toFixed(0) + " shares" : "--");
            html += tdGridItem("YES Liquidity", t.arb_yes_liquidity != null ? t.arb_yes_liquidity.toFixed(0) : "--");
            html += tdGridItem("NO Liquidity", t.arb_no_liquidity != null ? t.arb_no_liquidity.toFixed(0) : "--");
        }

        html += "</div></div>";

        // Resolution section (only if resolved)
        if (t.resolved) {
            var finalPnl = t.net_profit || 0;
            var wonLost = finalPnl >= 0 ? "won" : "lost";

            html += '<div class="td-section">';
            html += '<div class="td-section-title">Resolution</div>';
            html += '<div class="td-resolution">';
            html += '<div class="td-resolution-row"><span class="td-resolution-label">End YES Price</span><span class="td-resolution-value mono">' + fmtPrice(t.end_yes_price) + "</span></div>";
            html += '<div class="td-resolution-row"><span class="td-resolution-label">End NO Price</span><span class="td-resolution-value mono">' + fmtPrice(t.end_no_price) + "</span></div>";
            html += '<div class="td-resolution-row"><span class="td-resolution-label">Resolved At</span><span class="td-resolution-value mono">' + fmtTime(t.resolution_time) + "</span></div>";
            html += '<div class="td-resolution-row"><span class="td-resolution-label">Final P&L</span><span class="td-resolution-value mono" style="color: var(' + (finalPnl >= 0 ? "--green" : "--red") + ')">' + fmtUsd(finalPnl) + "</span></div>";
            html += '<div class="td-resolution-row"><span class="td-resolution-label">Result</span><span class="td-win-badge ' + wonLost + '">' + wonLost.toUpperCase() + "</span></div>";
            html += "</div></div>";
        }

        // Price chart
        var hasBefore = t.price_history_before && t.price_history_before.length > 0;
        var hasAfter  = t.price_history_after && t.price_history_after.length > 0;

        if (hasBefore || hasAfter) {
            html += '<div class="td-section td-chart-section">';
            html += '<div class="td-chart-header">';
            html += '<span class="td-section-title" style="margin-bottom:0; border-bottom:none; padding-bottom:0">Price History</span>';
            html += '<div class="td-chart-legend">';
            html += '<span class="legend-item"><span class="legend-dot yes-dot"></span>YES</span>';
            html += '<span class="legend-item"><span class="legend-dot no-dot"></span>NO</span>';
            html += '<span class="legend-item"><span class="legend-dot combined-dot"></span>Combined</span>';
            html += "</div></div>";
            html += '<div class="td-chart-container"><canvas id="trade-detail-chart"></canvas></div>';
            html += "</div>";
        }

        tradeDetailBody.innerHTML = html;

        // Draw chart if data exists
        if (hasBefore || hasAfter) {
            var canvas = document.getElementById("trade-detail-chart");
            if (canvas) {
                tradeChartCtx = canvas.getContext("2d");
                drawTradeChart(t);
            }
        }
    }

    function tdGridItem(label, value) {
        return '<div class="td-grid-item"><span class="td-grid-label">' + label + '</span><span class="td-grid-value">' + value + "</span></div>";
    }

    function drawTradeChart(t) {
        if (!tradeChartCtx) return;

        var before = t.price_history_before || [];
        var after  = t.price_history_after || [];
        var history = before.concat(after);
        var entryIndex = before.length;

        if (history.length < 2) return;

        var canvas = tradeChartCtx.canvas;
        var dpr = window.devicePixelRatio || 1;
        var rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        tradeChartCtx.scale(dpr, dpr);

        var W = rect.width;
        var H = rect.height;
        var pad = { top: 12, right: 12, bottom: 24, left: 50 };
        var plotW = W - pad.left - pad.right;
        var plotH = H - pad.top - pad.bottom;

        tradeChartCtx.clearRect(0, 0, W, H);

        // Extract series
        var yesData = history.map(function (p) { return p.yes_ask; });
        var noData  = history.map(function (p) { return p.no_ask; });
        var combData = history.map(function (p) { return p.combined_ask; });

        var allVals = yesData.concat(noData, combData).filter(function (v) { return v != null; });
        if (allVals.length < 2) return;

        var minV = Math.min.apply(null, allVals);
        var maxV = Math.max.apply(null, allVals);
        var range = maxV - minV;
        minV -= range * 0.1 || 0.01;
        maxV += range * 0.1 || 0.01;

        var len = history.length;
        var toX = function (i) { return pad.left + (i / (len - 1)) * plotW; };
        var toY = function (v) { return pad.top + (1 - (v - minV) / (maxV - minV)) * plotH; };

        // Grid lines
        tradeChartCtx.strokeStyle = "rgba(42, 42, 53, 0.6)";
        tradeChartCtx.lineWidth = 1;
        var gridLines = 4;
        for (var gi = 0; gi <= gridLines; gi++) {
            var gy = pad.top + (gi / gridLines) * plotH;
            tradeChartCtx.beginPath();
            tradeChartCtx.moveTo(pad.left, gy);
            tradeChartCtx.lineTo(W - pad.right, gy);
            tradeChartCtx.stroke();

            var gval = maxV - (gi / gridLines) * (maxV - minV);
            tradeChartCtx.fillStyle = "rgba(136, 136, 160, 0.6)";
            tradeChartCtx.font = "10px 'JetBrains Mono', monospace";
            tradeChartCtx.textAlign = "right";
            tradeChartCtx.fillText("$" + gval.toFixed(2), pad.left - 6, gy + 3);
        }

        // Draw series helper
        function drawLineSeries(values, color, width) {
            tradeChartCtx.strokeStyle = color;
            tradeChartCtx.lineWidth = width;
            tradeChartCtx.lineJoin = "round";
            tradeChartCtx.lineCap = "round";
            tradeChartCtx.beginPath();
            var started = false;
            for (var si = 0; si < values.length; si++) {
                if (values[si] == null) continue;
                var sx = toX(si);
                var sy = toY(values[si]);
                if (!started) { tradeChartCtx.moveTo(sx, sy); started = true; }
                else tradeChartCtx.lineTo(sx, sy);
            }
            tradeChartCtx.stroke();
        }

        drawLineSeries(combData, "#a855f7", 2);
        drawLineSeries(yesData, "#22c55e", 1.5);
        drawLineSeries(noData, "#ef4444", 1.5);

        // Entry vertical dashed line
        if (entryIndex > 0 && entryIndex < len) {
            var ex = toX(entryIndex);
            tradeChartCtx.setLineDash([4, 4]);
            tradeChartCtx.strokeStyle = "rgba(255, 255, 255, 0.3)";
            tradeChartCtx.lineWidth = 1;
            tradeChartCtx.beginPath();
            tradeChartCtx.moveTo(ex, pad.top);
            tradeChartCtx.lineTo(ex, pad.top + plotH);
            tradeChartCtx.stroke();
            tradeChartCtx.setLineDash([]);

            // Label
            tradeChartCtx.fillStyle = "rgba(255, 255, 255, 0.5)";
            tradeChartCtx.font = "9px 'JetBrains Mono', monospace";
            tradeChartCtx.textAlign = "center";
            tradeChartCtx.fillText("ENTRY", ex, pad.top - 2);
        }

        // Resolution marker (end of chart if resolved)
        if (t.resolved && after.length > 0) {
            var rx = toX(len - 1);
            tradeChartCtx.setLineDash([4, 4]);
            tradeChartCtx.strokeStyle = "rgba(234, 179, 8, 0.4)";
            tradeChartCtx.lineWidth = 1;
            tradeChartCtx.beginPath();
            tradeChartCtx.moveTo(rx, pad.top);
            tradeChartCtx.lineTo(rx, pad.top + plotH);
            tradeChartCtx.stroke();
            tradeChartCtx.setLineDash([]);

            tradeChartCtx.fillStyle = "rgba(234, 179, 8, 0.6)";
            tradeChartCtx.font = "9px 'JetBrains Mono', monospace";
            tradeChartCtx.textAlign = "center";
            tradeChartCtx.fillText("RESOLVED", rx, pad.top - 2);
        }

        // $1.00 reference line
        if (minV < 1.0 && maxV > 1.0) {
            tradeChartCtx.setLineDash([4, 4]);
            tradeChartCtx.strokeStyle = "rgba(136, 136, 160, 0.3)";
            tradeChartCtx.lineWidth = 1;
            tradeChartCtx.beginPath();
            var refY = toY(1.0);
            tradeChartCtx.moveTo(pad.left, refY);
            tradeChartCtx.lineTo(W - pad.right, refY);
            tradeChartCtx.stroke();
            tradeChartCtx.setLineDash([]);

            tradeChartCtx.fillStyle = "rgba(136, 136, 160, 0.5)";
            tradeChartCtx.font = "10px 'JetBrains Mono', monospace";
            tradeChartCtx.textAlign = "left";
            tradeChartCtx.fillText("$1.00", W - pad.right + 4, refY + 3);
        }
    }

    function initTradeDetail() {
        $("#trade-detail-close").addEventListener("click", closeTradeDetail);
        tradeDetailOverlay.addEventListener("click", closeTradeDetail);

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && tradeDetailDrawer.classList.contains("open")) {
                closeTradeDetail();
            }
        });
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
        SPIKE_THRESHOLD: 0.15,
        ARB_ENABLED: true,
    };

    const BOOL_FIELDS = new Set([
        "DRY_RUN", "AUTO_EXECUTE", "USE_WEBSOCKET", "STOP_LOSS_ENABLED", "ARB_ENABLED",
    ]);

    const INT_FIELDS = new Set([
        "MAX_CONCURRENT_POSITIONS", "ARB_COOLDOWN_SECONDS",
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
        // Load update channel separately (not a bot setting)
        loadUpdateChannel();
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

        // Update channel & manual check
        initUpdateSettings();
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

    // ── Update Settings (channel select + manual check) ────────────────

    async function fetchUpdateStatus() {
        try {
            const res = await fetch("/api/update-status");
            if (!res.ok) return null;
            return await res.json();
        } catch (e) {
            return null;
        }
    }

    async function pollUpdateStatus(onDone) {
        let attempts = 0;
        const poll = setInterval(async () => {
            attempts++;
            const data = await fetchUpdateStatus();
            if (!data || attempts >= 10 || !data.checking) {
                clearInterval(poll);
                onDone(data);
            }
        }, 500);
    }

    function showUpdateResult(data) {
        const statusEl = $("#update-check-status");
        if (!statusEl) return;
        statusEl.className = "update-check-status";

        if (!data || data.error) {
            statusEl.textContent = "Check failed";
            statusEl.classList.add("status-error");
        } else if (data.available) {
            statusEl.textContent = "Update available: v" + data.latest_version;
            statusEl.classList.add("status-available");
            showUpdateBanner(data.latest_version);
        } else {
            statusEl.textContent = "Up to date" + (data.current_version ? " (v" + data.current_version + ")" : "");
        }
    }

    function initUpdateSettings() {
        const channelSelect = $("#update-channel-select");
        const checkBtn = $("#update-check-btn");
        const statusEl = $("#update-check-status");

        if (channelSelect) {
            channelSelect.addEventListener("change", async () => {
                statusEl.className = "update-check-status";
                statusEl.textContent = "Switching...";
                try {
                    await fetch("/api/update-channel", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ channel: channelSelect.value }),
                    });
                    pollUpdateStatus(showUpdateResult);
                } catch (e) {
                    statusEl.textContent = "Check failed";
                    statusEl.className = "update-check-status status-error";
                }
            });
        }

        if (checkBtn) {
            checkBtn.addEventListener("click", async () => {
                checkBtn.disabled = true;
                checkBtn.textContent = "Checking...";
                statusEl.className = "update-check-status";
                statusEl.textContent = "";
                try {
                    await fetch("/api/update-check", { method: "POST" });
                    pollUpdateStatus((data) => {
                        showUpdateResult(data);
                        checkBtn.disabled = false;
                        checkBtn.textContent = "Check for Updates";
                    });
                } catch (e) {
                    statusEl.textContent = "Check failed";
                    statusEl.className = "update-check-status status-error";
                    checkBtn.disabled = false;
                    checkBtn.textContent = "Check for Updates";
                }
            });
        }
    }

    async function loadUpdateChannel() {
        const data = await fetchUpdateStatus();
        if (data && data.channel) {
            const channelSelect = $("#update-channel-select");
            if (channelSelect) channelSelect.value = data.channel;
        }
    }

    // ── Install Modal ─────────────────────────────────────────────────────

    function openInstallModal() {
        const overlay = $("#install-overlay");
        if (overlay) overlay.classList.add("open");
    }

    function closeInstallModal() {
        const overlay = $("#install-overlay");
        if (overlay) overlay.classList.remove("open");
    }

    function initInstallModal() {
        const cancelBtn = $("#install-cancel");
        const confirmBtn = $("#install-confirm");
        const overlay = $("#install-overlay");

        if (cancelBtn) cancelBtn.addEventListener("click", closeInstallModal);
        if (overlay) {
            overlay.addEventListener("click", (e) => {
                if (e.target === overlay) closeInstallModal();
            });
        }

        if (confirmBtn) {
            confirmBtn.addEventListener("click", async () => {
                confirmBtn.disabled = true;
                confirmBtn.textContent = "Installing\u2026";
                try {
                    await fetch("/api/update-install", { method: "POST" });
                } catch {}
                // App will restart — if still here after 10s, something went wrong
                setTimeout(() => {
                    confirmBtn.textContent = "Install failed \u2014 restart manually";
                    confirmBtn.disabled = false;
                }, 10000);
            });
        }

        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && overlay && overlay.classList.contains("open")) {
                closeInstallModal();
            }
        });
    }

    // ── Update Check ─────────────────────────────────────────────────────

    let updatePollTimer = null;

    async function initUpdateCheck() {
        // Load beta warning suppression preference
        try {
            const cfgRes = await fetch("/api/config");
            const cfg = await cfgRes.json();
            betaWarningSuppressed = !!cfg.suppress_beta_warning;
        } catch {}

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

            // Track current channel
            if (data.channel) currentChannel = data.channel;

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

            showUpdateBanner(data.latest_version);

            // Stop polling once banner is shown
            if (updatePollTimer) {
                clearInterval(updatePollTimer);
                updatePollTimer = null;
            }
        } catch (e) {
            // Silently ignore — update check is non-critical
        }
    }

    let _bannerShown = false;
    function showUpdateBanner(version) {
        const banner = $("#update-banner");
        if (!banner || _bannerShown) return;
        _bannerShown = true;

        $("#update-version").textContent = "v" + version;
        banner.hidden = false;

        const downloadBtn = $("#update-download");
        const progressBar = $("#update-progress");
        const progressFill = $("#update-progress-fill");
        const progressText = $("#update-progress-text");
        const installBtn = $("#update-install-btn");
        const betaWarning = $("#beta-warning");
        const betaDontShow = $("#beta-dont-show");

        // Show beta warning if on beta channel and not suppressed
        if (currentChannel === "beta" && !betaWarningSuppressed) {
            if (betaWarning) betaWarning.hidden = false;
        }

        // Download button click
        downloadBtn.addEventListener("click", async function handler(e) {
            e.preventDefault();
            downloadBtn.removeEventListener("click", handler);
            downloadBtn.hidden = true;
            progressBar.hidden = false;

            try {
                const dlRes = await fetch("/api/update-download", { method: "POST" });
                const dlData = await dlRes.json();
                if (!dlData.ok) {
                    progressBar.hidden = true;
                    downloadBtn.hidden = true;
                    // No DMG available — show fallback link
                    const fallback = document.createElement("a");
                    fallback.href = "https://github.com/luvskyy/skipolybot/releases/latest";
                    fallback.target = "_blank";
                    fallback.className = "update-download-btn";
                    fallback.textContent = "Download from GitHub";
                    downloadBtn.parentNode.insertBefore(fallback, downloadBtn.nextSibling);
                    return;
                }
            } catch (err) {
                progressBar.hidden = true;
                downloadBtn.hidden = false;
                downloadBtn.textContent = "Retry Download";
                return;
            }

            // Poll progress
            const pollId = setInterval(async () => {
                try {
                    const res = await fetch("/api/update-download-progress");
                    const data = await res.json();

                    const pct = Math.round(data.progress * 100);
                    progressFill.style.width = pct + "%";

                    if (data.total_bytes > 0) {
                        const mb = (data.downloaded_bytes / 1048576).toFixed(1);
                        const totalMb = (data.total_bytes / 1048576).toFixed(1);
                        progressText.textContent = mb + " / " + totalMb + " MB";
                    } else {
                        progressText.textContent = pct + "%";
                    }

                    if (data.done) {
                        clearInterval(pollId);
                        progressBar.hidden = true;
                        installBtn.hidden = false;
                    }
                    if (data.error) {
                        clearInterval(pollId);
                        progressBar.hidden = true;
                        downloadBtn.hidden = false;
                        downloadBtn.textContent = "Retry Download";
                    }
                } catch {
                    clearInterval(pollId);
                    progressBar.hidden = true;
                    downloadBtn.hidden = false;
                    downloadBtn.textContent = "Retry Download";
                }
            }, 500);
        });

        // Install button click — show warning modal
        installBtn.addEventListener("click", () => {
            openInstallModal();
        });

        // Beta "don't show again" checkbox
        if (betaDontShow) {
            betaDontShow.addEventListener("change", async () => {
                await fetch("/api/suppress-beta-warning", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ suppress: betaDontShow.checked }),
                });
                if (betaDontShow.checked) {
                    betaWarningSuppressed = true;
                }
            });
        }

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
        initTradeDetail();
        initUninstall();
        initInstallModal();
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

"""
Dashboard — live view of trades, PnL, and wallet.

UI only. Reads Trade/TelegramMessage rows that the bot already writes;
derives SL progression from the bot's deterministic target-hit rule:
  - 0 targets hit   → SL = original stop_loss
  - 1 target hit    → SL = entry (break-even)
  - N targets hit   → SL = targets[N-2]
(matches core/trade_manager.py:336-342)
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["Dashboard"])


def _sl_progression(td: dict) -> dict:
    """Derive current effective SL + history from current_target_idx."""
    idx = int(td.get("current_target_idx") or 0)
    initial_sl = td.get("stop_loss")
    entry = td.get("entry_price")
    targets = td.get("targets") or []

    history: list[dict[str, Any]] = [
        {"stage": "INITIAL", "label": "Original", "price": initial_sl, "after_target": None}
    ]
    if idx >= 1 and entry is not None:
        history.append({"stage": "BREAK_EVEN", "label": "Break-even", "price": entry, "after_target": 1})
    # After each subsequent target hit, SL locks to previous target
    for k in range(2, idx + 1):
        if k - 2 < len(targets):
            history.append({
                "stage": f"TARGET_{k-1}_LOCK",
                "label": f"Locked at T{k-1}",
                "price": targets[k - 2],
                "after_target": k,
            })

    current = history[-1]
    return {
        "effective_sl": current["price"],
        "sl_stage": current["stage"],
        "sl_label": current["label"],
        "sl_history": history,
        "sl_moves": max(0, len(history) - 1),
    }


@router.get("/dashboard/data", response_class=JSONResponse)
async def dashboard_data():
    from .routes import get_trade_manager
    from ..database.repository import get_db_session, TradeRepository
    from ..avantis.prices import get_price

    trades_list = []
    stats = {
        "active": 0, "closed": 0, "error": 0, "total": 0,
        "usdc_balance": None, "eth_balance": None,
        "realized_pnl_usd": 0.0, "open_pnl_usd": 0.0, "total_pnl_usd": 0.0,
        "live_untracked": 0,
    }

    # Fetch current prices (for PnL fallback if Lighter uPnL unavailable)
    prices = {}
    for sym in ["ETHUSDT", "BTCUSDT"]:
        try:
            prices[sym] = await get_price(sym)
        except Exception:
            pass

    # Fetch LIVE positions from Lighter — this is the source of truth for
    # what's actually open. DB can lag behind (reconciliation every N seconds).
    live_positions: list[dict] = []
    try:
        tm = get_trade_manager()
        if tm and tm.client and hasattr(tm.client, "get_open_positions"):
            live = await tm.client.get_open_positions()
            if live:
                live_positions = live
    except Exception:
        live_positions = []

    # Index live positions by pair_index — Lighter merges per pair so there's
    # at most one position per pair_index (direction derived from sign).
    live_by_pair: dict[int, dict] = {}
    for p in live_positions:
        try:
            pi = int(p.get("pairIndex", -1))
            if pi >= 0:
                live_by_pair[pi] = p
        except Exception:
            continue

    matched_pairs: set[int] = set()

    try:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            trades = await repo.get_recent_trades(limit=100)
            for t in trades:
                td = t.to_dict()

                price_sym = t.symbol.upper()
                if not price_sym.endswith("USDT"):
                    price_sym = price_sym.replace("USD", "") + "USDT"
                current_price = prices.get(price_sym)

                td["current_price"] = round(current_price, 2) if current_price else None
                td["current_target_idx"] = t.current_target_idx or 0

                # SL progression (derived from current_target_idx)
                td.update(_sl_progression(td))

                status_val = t.status.value
                is_active = status_val in ("active", "opening", "pending")
                is_long = t.direction.value == "long"

                # Attach live Lighter data if the position is actually still open
                live = live_by_pair.get(t.pair_index) if t.pair_index is not None else None
                live_sign_matches = False
                if live is not None:
                    live_is_long = int(live.get("sign", 0)) == 1
                    live_sign_matches = (live_is_long == is_long)

                if live is not None and live_sign_matches and is_active:
                    matched_pairs.add(t.pair_index)
                    try:
                        upnl = float(live.get("unrealized_pnl") or 0.0)
                    except (TypeError, ValueError):
                        upnl = 0.0
                    try:
                        live_size = float(live.get("position") or 0.0)
                    except (TypeError, ValueError):
                        live_size = 0.0
                    try:
                        live_entry = float(live.get("avg_entry_price") or t.entry_price)
                    except (TypeError, ValueError):
                        live_entry = t.entry_price

                    td["live"] = {
                        "size": live_size,
                        "entry": live_entry,
                        "unrealized_pnl": upnl,
                        "liquidation_price": live.get("liquidation_price"),
                        "allocated_margin": live.get("allocated_margin"),
                    }
                    # Use Lighter's uPnL as the authoritative PnL
                    td["pnl_usd"] = round(upnl, 2)
                    pnl_pct = (upnl / t.position_size_usd * 100) if t.position_size_usd else 0.0
                    td["pnl_pct"] = round(pnl_pct, 2)
                    stats["open_pnl_usd"] += upnl
                elif is_active and current_price:
                    # Active in DB but no live match (stale) — compute from price feed.
                    if is_long:
                        pnl_pct = ((current_price - t.entry_price) / t.entry_price) * 100 * t.leverage
                        pnl_usd = (current_price - t.entry_price) / t.entry_price * t.position_size_usd * t.leverage
                    else:
                        pnl_pct = ((t.entry_price - current_price) / t.entry_price) * 100 * t.leverage
                        pnl_usd = (t.entry_price - current_price) / t.entry_price * t.position_size_usd * t.leverage
                    td["pnl_usd"] = round(pnl_usd, 2)
                    td["pnl_pct"] = round(pnl_pct, 2)
                    td["live"] = None
                    td["stale"] = True  # DB says active but Lighter has nothing
                    stats["open_pnl_usd"] += pnl_usd
                else:
                    # Closed or error — use stored realized PnL
                    rp = td.get("realized_pnl_usd")
                    if rp is not None:
                        td["pnl_usd"] = round(float(rp), 2)
                        if t.position_size_usd:
                            td["pnl_pct"] = round(float(rp) / t.position_size_usd * 100, 2)
                        else:
                            td["pnl_pct"] = None
                    else:
                        td["pnl_usd"] = None
                        td["pnl_pct"] = None

                trades_list.append(td)
                stats["total"] += 1
                if is_active:
                    stats["active"] += 1
                elif status_val == "closed":
                    stats["closed"] += 1
                    if td.get("realized_pnl_usd") is not None:
                        stats["realized_pnl_usd"] += float(td["realized_pnl_usd"])
                elif status_val == "error":
                    stats["error"] += 1
    except Exception:
        pass

    # Surface any LIVE positions that aren't linked to an active DB trade
    # (e.g. manually opened on Lighter, or DB got cleared). Shown as read-only rows.
    untracked = []
    for pi, live in live_by_pair.items():
        if pi in matched_pairs:
            continue
        try:
            size = abs(float(live.get("position") or 0))
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        is_long = int(live.get("sign", 0)) == 1
        try:
            entry = float(live.get("avg_entry_price") or 0)
        except (TypeError, ValueError):
            entry = 0.0
        try:
            upnl = float(live.get("unrealized_pnl") or 0)
        except (TypeError, ValueError):
            upnl = 0.0
        untracked.append({
            "pair_index": pi,
            "symbol": live.get("symbol") or f"pair#{pi}",
            "direction": "long" if is_long else "short",
            "size": size,
            "entry_price": entry,
            "unrealized_pnl": round(upnl, 2),
            "liquidation_price": live.get("liquidation_price"),
            "allocated_margin": live.get("allocated_margin"),
        })
        stats["open_pnl_usd"] += upnl
    stats["live_untracked"] = len(untracked)

    stats["open_pnl_usd"] = round(stats["open_pnl_usd"], 2)
    stats["realized_pnl_usd"] = round(stats["realized_pnl_usd"], 2)
    stats["total_pnl_usd"] = round(stats["open_pnl_usd"] + stats["realized_pnl_usd"], 2)
    stats["live_open_count"] = len([p for p in live_positions if abs(float(p.get("position") or 0)) > 0])

    try:
        tm = get_trade_manager()
        if tm and tm.client:
            stats["usdc_balance"] = round(await tm.client.get_usdc_balance(), 2)
            stats["eth_balance"] = round(float(await tm.client.get_eth_balance()), 6)
    except Exception:
        pass

    # Telegram status
    try:
        from ..telegram_listener import telegram_status
        stats["telegram"] = dict(telegram_status)
    except Exception:
        stats["telegram"] = {"connected": False, "error": "import failed"}

    # Recent Telegram messages (collapse edits under their original message_id)
    tg_messages = []
    try:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            msgs = await repo.get_telegram_messages(limit=80)
            # Group by message_id — original + edits in one entry
            by_id: dict[int, dict[str, Any]] = {}
            for m in msgs:
                item = by_id.setdefault(m.message_id, {
                    "message_id": m.message_id,
                    "text": "",
                    "edits": [],
                    "is_signal": False,
                    "block_reason": None,
                    "received_at": None,
                    "last_at": None,
                })
                ts = m.received_at.isoformat() if m.received_at else None
                if m.is_edit:
                    item["edits"].append({"text": (m.text or "")[:500], "at": ts})
                else:
                    item["text"] = (m.text or "")[:500]
                    item["received_at"] = ts
                item["is_signal"] = item["is_signal"] or m.is_signal
                if m.block_reason:
                    item["block_reason"] = m.block_reason
                if ts and (item["last_at"] is None or ts > item["last_at"]):
                    item["last_at"] = ts
            tg_messages = sorted(by_id.values(), key=lambda x: x["last_at"] or "", reverse=True)[:50]
    except Exception:
        pass

    stats["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"trades": trades_list, "stats": stats, "messages": tg_messages, "untracked": untracked}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return _html()


def _html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Avantis Bot</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script>
    tailwind.config = { theme: { extend: { fontFamily: { sans:['Inter','sans-serif'], mono:['JetBrains Mono','monospace'] } } } }
  </script>
  <style>
    body { font-family:'Inter',sans-serif; background:#09090b; color:#e4e4e7; }
    .mono { font-family:'JetBrains Mono',monospace; }
    .card { background:#18181b; border:1px solid #27272a; border-radius:12px; }
    .card-glow { box-shadow: 0 0 30px -8px rgba(59,130,246,0.15); border-color: rgba(59,130,246,0.3); }
    .pulse-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
    .pulse-green { background:#4ade80; box-shadow:0 0 6px #4ade80; animation:pulse 2s infinite; }
    .pulse-red { background:#f87171; box-shadow:0 0 6px #f87171; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.45} }
    .badge { display:inline-flex; align-items:center; padding:2px 8px; border-radius:9999px; font-size:11px; font-weight:600; letter-spacing:0.4px; }
    .badge-long { background:rgba(34,197,94,0.15); color:#4ade80; }
    .badge-short { background:rgba(239,68,68,0.15); color:#f87171; }
    .badge-active { background:rgba(59,130,246,0.15); color:#60a5fa; }
    .badge-closed { background:rgba(113,113,122,0.15); color:#a1a1aa; }
    .badge-error { background:rgba(239,68,68,0.2); color:#fca5a5; }
    .badge-pending { background:rgba(234,179,8,0.15); color:#facc15; }

    /* Price scale */
    .scale { position:relative; height:58px; padding-top:8px; }
    .scale-track { position:absolute; left:12px; right:12px; top:34px; height:2px;
      background:linear-gradient(to right, rgba(239,68,68,0.35) 0%, rgba(113,113,122,0.4) 20%, rgba(113,113,122,0.4) 80%, rgba(74,222,128,0.35) 100%); border-radius:2px; }
    .marker { position:absolute; transform:translateX(-50%); top:18px; display:flex; flex-direction:column; align-items:center; }
    .marker-dot { width:12px; height:12px; border-radius:50%; border:2px solid #09090b; }
    .marker-label { position:absolute; top:-12px; font-size:10px; font-weight:600; white-space:nowrap; color:#71717a; }
    .marker-price { position:absolute; top:18px; font-size:10px; font-family:'JetBrains Mono',monospace; color:#a1a1aa; white-space:nowrap; }
    .m-sl { background:#f87171; }
    .m-sl-eff { background:#fb923c; border-color:#09090b; box-shadow:0 0 10px rgba(251,146,60,0.6); }
    .m-entry { background:#e4e4e7; }
    .m-target { background:#3f3f46; }
    .m-target-hit { background:#4ade80; box-shadow:0 0 6px #4ade80; }
    .m-current { width:0; height:0; border-left:6px solid transparent; border-right:6px solid transparent; border-top:10px solid #60a5fa; filter:drop-shadow(0 0 4px rgba(96,165,250,0.7)); }
    .m-current-wrap { top:-2px; }

    /* SL callout below scale */
    .sl-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:12px; }
    .sl-chip { display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:4px; font-weight:600; font-size:11px; }
    .sl-chip.initial { background:rgba(248,113,113,0.12); color:#fca5a5; }
    .sl-chip.be { background:rgba(74,222,128,0.15); color:#86efac; }
    .sl-chip.lock { background:rgba(251,146,60,0.15); color:#fdba74; }

    /* Badges on SL column */
    .sl-flash { animation: slFlash 3s ease-out; }
    @keyframes slFlash { 0% { background: rgba(251,146,60,0.2); } 100% { background: transparent; } }

    /* History table polish */
    .hist-row:hover { background:rgba(255,255,255,0.02); }

    /* TG feed */
    .edit-line { padding-left:14px; border-left:2px solid rgba(234,179,8,0.4); margin-top:4px; font-size:11px; color:#d4d4d8; white-space:pre-line; }
    .edit-time { font-size:10px; color:#71717a; margin-right:6px; }

    /* Divider label */
    .sec-label { font-size:11px; letter-spacing:0.08em; font-weight:600; color:#71717a; text-transform:uppercase; }
  </style>
</head>
<body class="min-h-screen antialiased">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 py-6">

    <!-- Header -->
    <div class="flex items-center justify-between mb-4">
      <div>
        <h1 class="text-xl font-bold text-white tracking-tight">Avantis Auto-Trader</h1>
        <p class="text-zinc-500 text-xs mt-0.5">Telegram signals → Base chain perps</p>
      </div>
      <div class="flex items-center gap-3">
        <span id="ts" class="text-[11px] text-zinc-600 mono"></span>
        <button onclick="load()" class="text-xs px-3 py-1.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300 transition">Refresh</button>
      </div>
    </div>

    <!-- Telegram Status Bar -->
    <div id="tg-status" class="card px-4 py-2.5 mb-4 flex items-center gap-3 text-xs"></div>

    <!-- Stats -->
    <div id="stats" class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-6"></div>

    <!-- Active Positions (card-per-trade) -->
    <div class="flex items-center justify-between mb-3">
      <span class="sec-label">Active Positions</span>
      <span id="active-count" class="text-[11px] text-zinc-600 mono">0</span>
    </div>
    <div id="positions-container" class="space-y-3 mb-6">
      <div class="card p-8 text-center text-zinc-500 text-sm">Loading...</div>
    </div>

    <!-- Trade History -->
    <div class="flex items-center justify-between mb-3">
      <span class="sec-label">Trade History</span>
      <span id="history-count" class="text-[11px] text-zinc-600 mono"></span>
    </div>
    <div class="card overflow-hidden mb-6">
      <div class="overflow-x-auto max-h-[500px] overflow-y-auto" id="history-container"></div>
    </div>

    <!-- Telegram Feed -->
    <div class="flex items-center justify-between mb-3">
      <span class="sec-label">Telegram Feed</span>
      <span id="msg-count" class="text-[11px] text-zinc-600 mono"></span>
    </div>
    <div class="card overflow-hidden">
      <div id="messages-container" class="max-h-96 overflow-y-auto"></div>
    </div>

  </div>

  <script>
    function $(id) { return document.getElementById(id); }
    function esc(s) { if(s==null) return ''; const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
    function fmtUsd(n, d) { d = d == null ? 2 : d; if (n == null) return '—'; const s = n < 0 ? '-' : ''; return s + '$' + Math.abs(n).toFixed(d); }
    function fmtSigned(n, d) { d = d == null ? 2 : d; if (n == null) return '—'; const s = n >= 0 ? '+' : '-'; return s + '$' + Math.abs(n).toFixed(d); }
    function fmtAgo(iso) {
      if (!iso) return '—';
      const t = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z').getTime();
      const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
      if (s < 60) return s + 's';
      if (s < 3600) return Math.floor(s / 60) + 'm';
      if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
      return Math.floor(s / 86400) + 'd';
    }
    function statCard(label, value, opts) {
      opts = opts || {};
      const cls = opts.positive ? 'text-emerald-400' : opts.negative ? 'text-red-400' : opts.accent ? 'text-blue-400' : 'text-white';
      const glow = opts.accent ? ' card-glow' : '';
      return '<div class="card'+glow+' p-4"><p class="text-[11px] font-medium uppercase tracking-wider text-zinc-500 mb-1.5">'+label+'</p><p class="mono text-lg font-semibold '+cls+'">'+value+'</p></div>';
    }

    // Build the price scale (SL / Entry / Targets + current price marker)
    function priceScale(t) {
      const targets = Array.isArray(t.targets) ? t.targets : [];
      const hitIdx = t.current_target_idx || 0;
      const initSL = t.stop_loss;
      const effSL = t.effective_sl != null ? t.effective_sl : initSL;
      const entry = t.entry_price;
      const current = t.current_price;
      const isShort = t.direction === 'short';

      const values = [initSL, entry].concat(targets).filter(v => v != null && !isNaN(v));
      if (current != null && !isNaN(current)) values.push(current);
      if (effSL != null && !isNaN(effSL)) values.push(effSL);
      if (!values.length) return '<div class="scale"></div>';

      // Always render with profit direction to the RIGHT:
      //   LONG:  min on left (SL side), max on right (target side)
      //   SHORT: invert so lower (= profit) is on right, higher (= loss) is on left
      let min = Math.min.apply(null, values);
      let max = Math.max.apply(null, values);
      if (max - min < 1e-9) max = min + 1; // guard
      const pad = (max - min) * 0.04;
      min -= pad; max += pad;

      function pos(p) {
        if (p == null || isNaN(p)) return null;
        const norm = (p - min) / (max - min);
        return isShort ? (1 - norm) : norm;
      }

      const parts = [];
      parts.push('<div class="scale"><div class="scale-track"></div>');

      // Initial SL — faded if effective SL has moved
      const moved = effSL != null && initSL != null && Math.abs(effSL - initSL) > 1e-9;
      if (initSL != null) {
        const p = pos(initSL);
        if (p != null) {
          const op = moved ? '0.35' : '1';
          parts.push(
            '<div class="marker" style="left:'+(p*100).toFixed(2)+'%;opacity:'+op+';">' +
            '<span class="marker-label" style="color:#f87171;">SL·init</span>' +
            '<span class="marker-dot m-sl"></span>' +
            '<span class="marker-price">'+initSL.toFixed(2)+'</span>' +
            '</div>'
          );
        }
      }
      // Effective SL (current) — orange, glowing, shown only if moved
      if (moved && effSL != null) {
        const p = pos(effSL);
        if (p != null) {
          parts.push(
            '<div class="marker" style="left:'+(p*100).toFixed(2)+'%;">' +
            '<span class="marker-label" style="color:#fb923c;">SL·now</span>' +
            '<span class="marker-dot m-sl-eff"></span>' +
            '<span class="marker-price" style="color:#fdba74;">'+effSL.toFixed(2)+'</span>' +
            '</div>'
          );
        }
      }
      // Entry
      if (entry != null) {
        const p = pos(entry);
        if (p != null) {
          parts.push(
            '<div class="marker" style="left:'+(p*100).toFixed(2)+'%;">' +
            '<span class="marker-label" style="color:#e4e4e7;">Entry</span>' +
            '<span class="marker-dot m-entry"></span>' +
            '<span class="marker-price" style="color:#e4e4e7;">'+entry.toFixed(2)+'</span>' +
            '</div>'
          );
        }
      }
      // Targets
      for (let i = 0; i < targets.length; i++) {
        const p = pos(targets[i]);
        if (p == null) continue;
        const hit = i < hitIdx;
        parts.push(
          '<div class="marker" style="left:'+(p*100).toFixed(2)+'%;">' +
          '<span class="marker-label" style="color:'+(hit?'#4ade80':'#71717a')+';">T'+(i+1)+(hit?' ✓':'')+'</span>' +
          '<span class="marker-dot '+(hit?'m-target-hit':'m-target')+'"></span>' +
          '<span class="marker-price" style="color:'+(hit?'#86efac':'#71717a')+';">'+targets[i].toFixed(2)+'</span>' +
          '</div>'
        );
      }
      // Current price (blue triangle)
      if (current != null) {
        const p = pos(current);
        if (p != null) {
          parts.push(
            '<div class="marker m-current-wrap" style="left:'+(p*100).toFixed(2)+'%;">' +
            '<span class="marker-label" style="color:#60a5fa;">Now '+current.toFixed(2)+'</span>' +
            '<span class="m-current"></span>' +
            '</div>'
          );
        }
      }

      parts.push('</div>');
      return parts.join('');
    }

    // SL progression text row — makes SL changes impossible to miss
    function slProgressionRow(t) {
      const hist = Array.isArray(t.sl_history) ? t.sl_history : [];
      const eff = t.effective_sl;
      const init = t.stop_loss;
      const stage = t.sl_stage || 'INITIAL';

      if (stage === 'INITIAL') {
        return '<div class="sl-row"><span class="sl-chip initial">🛡 SL</span>' +
          '<span class="mono text-zinc-300">' + (init != null ? init.toFixed(2) : '—') + '</span>' +
          '<span class="text-zinc-600 text-[11px]">(original)</span></div>';
      }

      const stageChip = stage === 'BREAK_EVEN'
        ? '<span class="sl-chip be">✓ Break-even</span>'
        : '<span class="sl-chip lock">🔒 ' + esc(t.sl_label || stage) + '</span>';

      // Build the move chain: init → ... → eff
      const chain = hist.map(function(h, i) {
        const prc = h.price != null ? h.price.toFixed(2) : '—';
        const isCur = i === hist.length - 1;
        const cls = isCur ? 'text-amber-300 font-semibold' : 'text-zinc-500 line-through';
        return '<span class="mono '+cls+'">'+prc+'</span>';
      }).join('<span class="text-zinc-600"> → </span>');

      return '<div class="sl-row">' +
        stageChip +
        '<span class="text-zinc-500 text-[11px]">' + (hist.length - 1) + ' move' + (hist.length === 2 ? '' : 's') + ':</span>' +
        chain +
        '</div>';
    }

    // Untracked live position — exists on Lighter but no matching DB trade
    function renderUntrackedCard(p) {
      const dir = p.direction || '';
      const dirBadge = '<span class="badge badge-'+dir+'">'+dir.toUpperCase()+'</span>';
      const pnl = p.unrealized_pnl || 0;
      const pnlCls = pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
      const pnlTxt = (pnl >= 0 ? '+' : '−') + '$' + Math.abs(pnl).toFixed(2);
      return '<div class="card p-4 border-amber-500/30">' +
        '<div class="flex items-start justify-between mb-2">' +
          '<div class="flex items-center gap-2 flex-wrap">' +
            '<span class="font-bold text-white text-base">'+esc(p.symbol)+'</span>' +
            dirBadge +
            '<span class="badge badge-pending">LIVE · no DB match</span>' +
          '</div>' +
          '<div class="text-right">' +
            '<div class="mono text-lg font-bold '+pnlCls+'">'+pnlTxt+'</div>' +
            '<div class="text-[10px] text-zinc-500">unrealized</div>' +
          '</div>' +
        '</div>' +
        '<div class="flex gap-4 text-[11px] text-zinc-400 mono flex-wrap">' +
          '<span>Size: <span class="text-zinc-200">'+p.size.toFixed(4)+'</span></span>' +
          '<span>Entry: <span class="text-zinc-200">$'+(p.entry_price||0).toFixed(2)+'</span></span>' +
          (p.liquidation_price ? '<span>Liq: <span class="text-red-400">$'+parseFloat(p.liquidation_price).toFixed(2)+'</span></span>' : '') +
          (p.allocated_margin ? '<span>Margin: <span class="text-zinc-200">$'+parseFloat(p.allocated_margin).toFixed(2)+'</span></span>' : '') +
        '</div>' +
      '</div>';
    }

    // One card per active trade
    function renderCard(t) {
      const dir = t.direction || '';
      const dirBadge = '<span class="badge badge-'+dir+'">'+dir.toUpperCase()+'</span>';
      const statusCls = 'badge-' + (t.status || 'pending');
      const statusBadge = '<span class="badge '+statusCls+'">'+(t.status||'').toUpperCase()+'</span>';
      const staleBadge = t.stale ? '<span class="badge badge-error" title="DB says active, Lighter has no position — reconciling">⚠ STALE</span>' : '';
      const liveBadge = t.live ? '<span class="badge" style="background:rgba(74,222,128,0.1);color:#86efac;" title="Live on Lighter">● LIVE</span>' : '';
      const pnl = t.pnl_usd;
      const pnlCls = pnl == null ? 'text-zinc-500' : pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
      const pnlTxt = pnl == null ? '—' : (pnl >= 0 ? '+' : '−') + '$' + Math.abs(pnl).toFixed(2);
      const pnlPctTxt = t.pnl_pct == null ? '' : (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct.toFixed(2) + '%';
      const txShort = t.tx_hash ? t.tx_hash.slice(0, 10) + '…' : '—';
      const txLink = t.tx_hash && t.tx_hash !== 'DRY_RUN'
        ? '<a href="https://basescan.org/tx/0x' + t.tx_hash + '" target="_blank" class="text-blue-400/70 hover:text-blue-400 transition">' + txShort + '</a>'
        : '<span class="text-zinc-600">' + txShort + '</span>';

      const targets = Array.isArray(t.targets) ? t.targets : [];
      const hitIdx = t.current_target_idx || 0;
      const hitInfo = hitIdx > 0
        ? '<span class="text-emerald-400">🎯 ' + hitIdx + '/' + targets.length + ' hit</span>'
        : '<span class="text-zinc-500">0/' + targets.length + ' hit</span>';
      const nextTarget = hitIdx < targets.length ? targets[hitIdx] : null;
      const nextTxt = nextTarget != null
        ? '<span class="text-zinc-500">· Next T' + (hitIdx + 1) + ' at <span class="mono text-zinc-300">' + nextTarget.toFixed(2) + '</span></span>'
        : '<span class="text-zinc-500">· all targets hit</span>';

      // Live (Lighter) details row — position size, liq price, entry mismatch
      let liveRow = '';
      if (t.live) {
        const liveSize = (t.live.size != null) ? Math.abs(t.live.size).toFixed(4) : '—';
        const liqP = t.live.liquidation_price ? '$' + parseFloat(t.live.liquidation_price).toFixed(2) : '—';
        const margin = t.live.allocated_margin ? '$' + parseFloat(t.live.allocated_margin).toFixed(2) : '—';
        const entryMismatch = (t.live.entry && t.entry_price && Math.abs(t.live.entry - t.entry_price) / t.entry_price > 0.002)
          ? ' <span class="text-amber-400 text-[10px]">live entry $'+t.live.entry.toFixed(2)+' (merged)</span>' : '';
        liveRow = '<div class="flex items-center gap-4 mt-2 text-[11px] text-zinc-400 mono flex-wrap">' +
          '<span>● Live size <span class="text-zinc-200">'+liveSize+'</span>'+entryMismatch+'</span>' +
          '<span>Liq <span class="text-red-400">'+liqP+'</span></span>' +
          '<span>Margin <span class="text-zinc-200">'+margin+'</span></span>' +
          '</div>';
      }

      return '<div class="card p-4" data-tid="'+esc(t.id)+'">' +
        // Header row
        '<div class="flex items-start justify-between mb-3">' +
          '<div class="flex items-center gap-2 flex-wrap">' +
            '<span class="font-bold text-white text-base">'+esc(t.symbol)+'</span>' +
            dirBadge +
            '<span class="text-zinc-500 text-xs mono">'+t.leverage+'×</span>' +
            '<span class="text-zinc-600 text-xs">·</span>' +
            '<span class="text-zinc-400 text-xs">$'+t.position_size_usd+' col</span>' +
            '<span class="text-zinc-600 text-xs">·</span>' +
            statusBadge + liveBadge + staleBadge +
          '</div>' +
          '<div class="text-right">' +
            '<div class="mono text-lg font-bold '+pnlCls+'">'+pnlTxt+'</div>' +
            '<div class="text-[11px] '+pnlCls+' opacity-80">'+pnlPctTxt+(t.live?' · live':'')+'</div>' +
          '</div>' +
        '</div>' +
        // Price scale
        priceScale(t) +
        // SL progression row
        '<div class="mt-3">' + slProgressionRow(t) + '</div>' +
        // Live details (if present)
        liveRow +
        // Targets + metadata
        '<div class="flex items-center justify-between mt-2 text-[11px] text-zinc-500">' +
          '<div>' + hitInfo + ' ' + nextTxt + '</div>' +
          '<div>Opened ' + fmtAgo(t.opened_at) + ' ago · ' + txLink + '</div>' +
        '</div>' +
      '</div>';
    }

    // Compact history table
    function renderHistory(trades) {
      const sorted = trades.slice().sort(function(a,b) {
        const ta = a.opened_at || a.created_at || '';
        const tb = b.opened_at || b.created_at || '';
        return tb.localeCompare(ta);
      });
      if (!sorted.length) return '<div class="py-8 text-center text-zinc-600 text-sm">No trades yet</div>';

      const headers = ['Time','Pair','Side','Lev','Entry','Size','Targets','Eff. SL','PnL','Status'];
      const headerRow = '<tr class="sticky top-0 bg-zinc-900/80 backdrop-blur">' +
        headers.map(function(h){return '<th class="text-left py-2 px-3 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 whitespace-nowrap">'+h+'</th>';}).join('') + '</tr>';

      const body = sorted.map(function(t) {
        const time = (t.opened_at || t.created_at || '').slice(5,16).replace('T',' ') || '—';
        const dir = t.direction || '';
        const targets = Array.isArray(t.targets) ? t.targets : [];
        const hit = t.current_target_idx || 0;
        const targetsCell = hit > 0
          ? '<span class="text-emerald-400 mono font-semibold">'+hit+'/'+targets.length+'</span>'
          : '<span class="text-zinc-500 mono">0/'+targets.length+'</span>';
        const slMoved = t.effective_sl != null && t.stop_loss != null && Math.abs(t.effective_sl - t.stop_loss) > 1e-9;
        const slCell = slMoved
          ? '<span class="mono text-amber-300">'+(t.effective_sl!=null?t.effective_sl.toFixed(2):'—')+'</span><span class="text-[10px] text-zinc-600 mono ml-1 line-through">'+t.stop_loss.toFixed(2)+'</span>'
          : '<span class="mono text-zinc-400">'+(t.stop_loss!=null?t.stop_loss.toFixed(2):'—')+'</span>';
        const pnlHtml = t.pnl_usd != null
          ? '<span class="mono text-xs '+(t.pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400')+'">'+(t.pnl_usd >= 0 ? '+' : '−')+'$'+Math.abs(t.pnl_usd).toFixed(2)+'</span>'
          : '<span class="text-zinc-600">—</span>';
        return '<tr class="hist-row border-b border-zinc-800/50">' +
          '<td class="py-2 px-3 text-[11px] text-zinc-400 mono whitespace-nowrap">'+time+'</td>' +
          '<td class="py-2 px-3 text-xs font-semibold text-white">'+esc(t.symbol)+'</td>' +
          '<td class="py-2 px-3"><span class="badge badge-'+dir+'">'+dir.toUpperCase()+'</span></td>' +
          '<td class="py-2 px-3 text-[11px] text-zinc-400 mono">'+t.leverage+'×</td>' +
          '<td class="py-2 px-3 text-[11px] text-zinc-300 mono">'+(t.entry_price!=null?t.entry_price.toFixed(2):'—')+'</td>' +
          '<td class="py-2 px-3 text-[11px] text-zinc-400 mono">$'+t.position_size_usd+'</td>' +
          '<td class="py-2 px-3 text-[11px]">'+targetsCell+'</td>' +
          '<td class="py-2 px-3 text-[11px]">'+slCell+'</td>' +
          '<td class="py-2 px-3">'+pnlHtml+'</td>' +
          '<td class="py-2 px-3"><span class="badge badge-'+(t.status||'pending')+'">'+(t.status||'').toUpperCase()+'</span></td>' +
        '</tr>';
      }).join('');

      return '<table class="w-full"><thead>'+headerRow+'</thead><tbody>'+body+'</tbody></table>';
    }

    async function load() {
      try {
        const r = await fetch('/dashboard/data');
        const resp = await r.json();
        const trades = resp.trades || [];
        const stats = resp.stats || {};
        const messages = resp.messages || [];

        // Telegram status
        const tg = stats.telegram || {};
        const connected = !!tg.connected;
        const dotCls = connected ? 'pulse-green' : 'pulse-red';
        const statusText = connected
          ? 'Connected as <b>'+esc(tg.username)+'</b> · Watching <b>'+esc(tg.channel)+'</b>'
          : 'Disconnected' + (tg.error ? ' · '+esc(tg.error) : '');
        const lastMsg = tg.last_message_at ? ' · Last msg ' + fmtAgo(tg.last_message_at) + ' ago' : '';
        $('tg-status').innerHTML = '<span class="pulse-dot '+dotCls+'"></span> <span>'+statusText+lastMsg+'</span>';

        // Stats cards
        let cards = '';
        if (stats.usdc_balance != null) cards += statCard('USDC', '$'+stats.usdc_balance.toLocaleString(undefined,{minimumFractionDigits:2}), {accent:true});
        if (stats.eth_balance != null) cards += statCard('ETH', stats.eth_balance.toFixed(6), {accent:true});
        const liveCount = stats.live_open_count || 0;
        cards += statCard('Open (live)', liveCount + (stats.active != null && stats.active !== liveCount ? ' / DB '+stats.active : ''));
        cards += statCard('Closed', stats.closed || 0);
        const openPnl = stats.open_pnl_usd || 0;
        cards += statCard('Open uPnL', (openPnl >= 0 ? '+' : '−') + '$' + Math.abs(openPnl).toFixed(2), {positive: openPnl >= 0, negative: openPnl < 0});
        const realPnl = stats.realized_pnl_usd || 0;
        cards += statCard('Realized PnL', (realPnl >= 0 ? '+' : '−') + '$' + Math.abs(realPnl).toFixed(2), {positive: realPnl >= 0, negative: realPnl < 0});
        $('stats').innerHTML = cards;

        // Active positions (cards)
        const active = trades.filter(function(t){ return ['active','opening','pending'].indexOf(t.status) >= 0; });
        const untracked = resp.untracked || [];
        const totalActive = active.length + untracked.length;
        $('active-count').textContent = totalActive + ' position' + (totalActive === 1 ? '' : 's') +
          (untracked.length ? ' · ' + untracked.length + ' untracked live' : '');
        if (totalActive) {
          $('positions-container').innerHTML = active.map(renderCard).join('') + untracked.map(renderUntrackedCard).join('');
        } else {
          $('positions-container').innerHTML = '<div class="card p-8 text-center text-zinc-600 text-sm">No active positions</div>';
        }

        // History table
        $('history-container').innerHTML = renderHistory(trades);
        $('history-count').textContent = trades.length + ' trades';

        // Telegram feed (grouped: original + edits under one entry)
        if (messages.length) {
          $('msg-count').textContent = messages.length + ' messages';
          $('messages-container').innerHTML = messages.map(function(m) {
            const time = m.received_at ? m.received_at.slice(5,16).replace('T',' ') : (m.last_at ? m.last_at.slice(5,16).replace('T',' ') : '');
            const signalTag = m.is_signal ? '<span class="badge badge-active">SIGNAL</span> ' : '';
            const blockTag = m.block_reason ? '<span class="badge badge-error">'+esc(m.block_reason)+'</span> ' : '';
            const editCount = (m.edits || []).length;
            const editBadge = editCount > 0 ? '<span class="badge badge-pending">'+editCount+' edit'+(editCount===1?'':'s')+'</span> ' : '';
            const textPreview = esc((m.text || '').slice(0, 200)) + (m.text && m.text.length > 200 ? '…' : '');

            let editsHtml = '';
            if (editCount > 0) {
              editsHtml = (m.edits || []).map(function(e) {
                const etime = e.at ? e.at.slice(11,19) : '';
                return '<div class="edit-line"><span class="edit-time mono">'+etime+' edit →</span>'+esc((e.text||'').slice(0,300))+'</div>';
              }).join('');
            }

            return '<div class="px-4 py-3 border-b border-zinc-800/50 hover:bg-white/[0.02]">' +
              '<div class="flex items-center gap-2 mb-1 flex-wrap">' +
              '<span class="text-[11px] text-zinc-500 mono">'+time+'</span> ' +
              '<span class="text-[11px] text-zinc-600">#'+m.message_id+'</span> ' +
              editBadge + signalTag + blockTag +
              '</div>' +
              '<p class="text-xs text-zinc-400 leading-relaxed whitespace-pre-line">'+textPreview+'</p>' +
              editsHtml +
              '</div>';
          }).join('');
        } else {
          $('messages-container').innerHTML = '<div class="py-8 text-center text-zinc-600 text-sm">No messages received yet</div>';
        }

        $('ts').textContent = stats.timestamp || '';
      } catch (e) {
        $('positions-container').innerHTML = '<div class="card p-4 text-red-400 text-sm">'+esc(e.message)+'</div>';
      }
    }

    load();
    setInterval(load, 15000);
  </script>
</body>
</html>"""

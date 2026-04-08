"""
Dashboard — live view of trades, PnL, and wallet.
"""

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["Dashboard"])


@router.get("/dashboard/data", response_class=JSONResponse)
async def dashboard_data():
    from .routes import get_trade_manager
    from ..database.repository import get_db_session, TradeRepository
    from ..avantis.pairs import get_price_symbol
    from ..avantis.prices import get_price

    trades_list = []
    stats = {
        "active": 0, "closed": 0, "error": 0, "total": 0,
        "usdc_balance": None, "eth_balance": None,
        "total_pnl_usd": 0, "open_pnl_usd": 0,
    }

    # Fetch current prices
    prices = {}
    for sym in ["ETHUSDT", "BTCUSDT"]:
        try:
            prices[sym] = await get_price(sym)
        except Exception:
            pass

    try:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            trades = await repo.get_recent_trades(limit=100)
            for t in trades:
                td = t.to_dict()

                # Enrich with current price and PnL
                price_sym = t.symbol.upper()
                if not price_sym.endswith("USDT"):
                    price_sym = price_sym.replace("USD", "") + "USDT"
                current_price = prices.get(price_sym)

                td["current_price"] = round(current_price, 2) if current_price else None
                td["current_target_idx"] = t.current_target_idx or 0

                # Calculate PnL for active trades
                if current_price and t.status.value in ("active", "opening"):
                    is_long = t.direction.value == "long"
                    if is_long:
                        pnl_pct = ((current_price - t.entry_price) / t.entry_price) * 100 * t.leverage
                        pnl_usd = (current_price - t.entry_price) / t.entry_price * t.position_size_usd * t.leverage
                    else:
                        pnl_pct = ((t.entry_price - current_price) / t.entry_price) * 100 * t.leverage
                        pnl_usd = (t.entry_price - current_price) / t.entry_price * t.position_size_usd * t.leverage
                    td["pnl_usd"] = round(pnl_usd, 2)
                    td["pnl_pct"] = round(pnl_pct, 2)
                    stats["open_pnl_usd"] += pnl_usd
                else:
                    td["pnl_usd"] = None
                    td["pnl_pct"] = None

                trades_list.append(td)
                stats["total"] += 1
                if t.status.value in ("active", "opening", "pending"):
                    stats["active"] += 1
                elif t.status.value == "closed":
                    stats["closed"] += 1
                elif t.status.value == "error":
                    stats["error"] += 1
    except Exception:
        pass

    stats["open_pnl_usd"] = round(stats["open_pnl_usd"], 2)

    try:
        tm = get_trade_manager()
        if tm.client:
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

    # Recent Telegram messages
    tg_messages = []
    try:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            msgs = await repo.get_telegram_messages(limit=50)
            for m in msgs:
                tg_messages.append({
                    "message_id": m.message_id,
                    "text": m.text[:500] if m.text else "",
                    "is_edit": m.is_edit,
                    "is_signal": m.is_signal,
                    "block_reason": m.block_reason,
                    "received_at": m.received_at.isoformat() if m.received_at else None,
                })
    except Exception:
        pass

    stats["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"trades": trades_list, "stats": stats, "messages": tg_messages}


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
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: { sans:['Inter','sans-serif'], mono:['JetBrains Mono','monospace'] },
        }
      }
    }
  </script>
  <style>
    body { font-family:'Inter',sans-serif; background:#09090b; color:#e4e4e7; }
    .mono { font-family:'JetBrains Mono',monospace; }
    .card { background:#18181b; border:1px solid #27272a; border-radius:12px; }
    .card-glow { box-shadow: 0 0 30px -8px rgba(59,130,246,0.15); border-color: rgba(59,130,246,0.3); }
    .tg-connected { color:#4ade80; }
    .tg-disconnected { color:#f87171; }
    .pulse-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
    .pulse-green { background:#4ade80; box-shadow:0 0 6px #4ade80; animation:pulse 2s infinite; }
    .pulse-red { background:#f87171; box-shadow:0 0 6px #f87171; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
    .badge { display:inline-flex; align-items:center; padding:2px 8px; border-radius:9999px; font-size:11px; font-weight:600; letter-spacing:0.5px; }
    .badge-long { background:rgba(34,197,94,0.15); color:#4ade80; }
    .badge-short { background:rgba(239,68,68,0.15); color:#f87171; }
    .badge-active { background:rgba(59,130,246,0.15); color:#60a5fa; }
    .badge-closed { background:rgba(113,113,122,0.15); color:#a1a1aa; }
    .badge-error { background:rgba(239,68,68,0.15); color:#f87171; }
    .badge-pending { background:rgba(234,179,8,0.15); color:#facc15; }
    .tp-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:4px; }
    .tp-hit { background:#4ade80; }
    .tp-pending { background:#3f3f46; }
    .tp-next { background:#60a5fa; animation: pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
    tr:hover { background: rgba(255,255,255,0.03); }
  </style>
</head>
<body class="min-h-screen antialiased">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 py-6">

    <!-- Header + Telegram Status -->
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

    <!-- Trades -->
    <div class="card overflow-hidden mb-6">
      <div class="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
        <h3 class="text-sm font-semibold text-zinc-400">Positions</h3>
        <span id="trade-count" class="text-xs text-zinc-600 mono"></span>
      </div>
      <div class="overflow-x-auto">
        <div id="loading" class="py-12 text-center text-zinc-500 text-sm">Loading...</div>
        <div id="trades-container" style="display:none"></div>
      </div>
    </div>

    <!-- Telegram Message Feed -->
    <div class="card overflow-hidden">
      <div class="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
        <h3 class="text-sm font-semibold text-zinc-400">Telegram Feed</h3>
        <span id="msg-count" class="text-xs text-zinc-600 mono"></span>
      </div>
      <div id="messages-container" class="max-h-96 overflow-y-auto"></div>
    </div>

  </div>

  <script>
    function $(id) { return document.getElementById(id); }
    function esc(s) { if(s==null) return ''; let d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

    function statCard(label, value, opts={}) {
      const cls = opts.positive ? 'text-emerald-400' : opts.negative ? 'text-red-400' : opts.accent ? 'text-blue-400' : 'text-white';
      const glow = opts.accent ? ' card-glow' : '';
      return '<div class="card'+glow+' p-4"><p class="text-[11px] font-medium uppercase tracking-wider text-zinc-500 mb-1.5">'+label+'</p><p class="mono text-lg font-semibold '+cls+'">'+value+'</p></div>';
    }

    function renderTargets(targets, currentIdx, direction) {
      if (!targets || !targets.length) return '<span class="text-zinc-600">—</span>';
      return targets.map((t, i) => {
        let dotCls = 'tp-pending';
        let label = '';
        if (i < currentIdx) { dotCls = 'tp-hit'; label = '✓'; }
        else if (i === currentIdx) { dotCls = 'tp-next'; }
        return '<span class="inline-flex items-center mr-2 text-xs mono" title="Target '+(i+1)+'">' +
          '<span class="tp-dot '+dotCls+'"></span>' +
          '<span class="'+(i < currentIdx ? 'text-emerald-400 line-through' : i === currentIdx ? 'text-blue-400' : 'text-zinc-500')+'">'+t+'</span>' +
          '</span>';
      }).join('');
    }

    function renderPnl(pnl_usd, pnl_pct) {
      if (pnl_usd == null) return '<span class="text-zinc-600">—</span>';
      const pos = pnl_usd >= 0;
      const cls = pos ? 'text-emerald-400' : 'text-red-400';
      const sign = pos ? '+' : '';
      return '<div class="'+cls+' mono text-xs leading-relaxed">' +
        '<div class="font-semibold">'+sign+'$'+pnl_usd.toFixed(2)+'</div>' +
        '<div class="text-[11px] opacity-75">'+sign+pnl_pct.toFixed(2)+'%</div>' +
        '</div>';
    }

    function renderStatus(status, currentIdx, numTargets) {
      let cls = 'badge-'+status;
      let label = status.toUpperCase();
      if (status === 'active' && currentIdx > 0) {
        label = 'PARTIAL ' + currentIdx + '/' + numTargets;
        cls = 'badge-active';
      }
      return '<span class="badge '+cls+'">'+label+'</span>';
    }

    function renderTrade(t) {
      const targets = Array.isArray(t.targets) ? t.targets : [];
      const dir = t.direction || '';
      const dirBadge = '<span class="badge badge-'+dir+'">'+dir.toUpperCase()+'</span>';
      const currentIdx = t.current_target_idx || 0;
      const txShort = t.tx_hash ? t.tx_hash.slice(0,8)+'...' : '—';
      const txLink = t.tx_hash && t.tx_hash !== 'DRY_RUN'
        ? '<a href="https://basescan.org/tx/0x'+t.tx_hash+'" target="_blank" class="text-blue-400/70 hover:text-blue-400 transition">'+txShort+'</a>'
        : '<span class="text-zinc-600">'+txShort+'</span>';
      const opened = t.opened_at ? t.opened_at.slice(5,16).replace('T',' ') : '—';

      return '<tr class="border-b border-zinc-800/50">' +
        '<td class="py-3 px-4"><div class="font-semibold text-white text-sm">'+t.symbol+'</div><div class="text-[11px] text-zinc-500 mt-0.5">'+t.leverage+'x · $'+t.position_size_usd+'</div></td>' +
        '<td class="py-3 px-3">'+dirBadge+'</td>' +
        '<td class="py-3 px-3 mono text-xs"><div class="text-zinc-300">'+t.entry_price+'</div>'+(t.current_price ? '<div class="text-[11px] text-zinc-500">now: '+t.current_price+'</div>' : '')+'</td>' +
        '<td class="py-3 px-3">'+renderTargets(targets, currentIdx, dir)+'</td>' +
        '<td class="py-3 px-3 mono text-xs text-zinc-400">'+t.stop_loss+'</td>' +
        '<td class="py-3 px-3">'+renderPnl(t.pnl_usd, t.pnl_pct)+'</td>' +
        '<td class="py-3 px-3">'+renderStatus(t.status, currentIdx, targets.length)+'</td>' +
        '<td class="py-3 px-3 mono text-[11px]">'+txLink+'</td>' +
        '<td class="py-3 px-3 text-[11px] text-zinc-500">'+opened+'</td>' +
      '</tr>';
    }

    async function load() {
      try {
        const r = await fetch('/dashboard/data');
        const {trades, stats, messages} = await r.json();

        // Telegram status bar
        const tg = stats.telegram || {};
        const connected = tg.connected;
        const dotCls = connected ? 'pulse-green' : 'pulse-red';
        const statusText = connected
          ? 'Connected as <b>'+esc(tg.username)+'</b> · Watching <b>'+esc(tg.channel)+'</b>'
          : 'Disconnected' + (tg.error ? ' · '+esc(tg.error) : '');
        const lastMsg = tg.last_message_at ? ' · Last msg: '+tg.last_message_at.slice(11,19)+' UTC' : '';
        $('tg-status').innerHTML = '<span class="pulse-dot '+dotCls+'"></span> <span>'
          + statusText + lastMsg + '</span>';

        // Stats
        let cards = '';
        if (stats.usdc_balance != null) cards += statCard('USDC', '$'+stats.usdc_balance.toLocaleString(undefined,{minimumFractionDigits:2}), {accent:true});
        if (stats.eth_balance != null) cards += statCard('ETH', stats.eth_balance.toFixed(6), {accent:true});
        cards += statCard('Active', stats.active);
        cards += statCard('Closed', stats.closed);
        const openPnl = stats.open_pnl_usd || 0;
        cards += statCard('Open PnL', (openPnl>=0?'+':'')+'$'+openPnl.toFixed(2), {positive:openPnl>=0, negative:openPnl<0});
        cards += statCard('Total', stats.total);
        $('stats').innerHTML = cards;

        // Trades table
        const headers = ['Pair','Side','Entry','Targets','SL','PnL','Status','TX','Opened'];
        const headerRow = '<tr>'+headers.map(h => '<th class="text-left py-2 px-3 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 whitespace-nowrap">'+h+'</th>').join('')+'</tr>';

        const activeTrades = trades.filter(t => ['active','opening','pending'].includes(t.status));
        const closedTrades = trades.filter(t => !['active','opening','pending'].includes(t.status));

        let html = '';
        if (activeTrades.length) {
          html += '<table class="w-full"><thead>'+headerRow+'</thead><tbody>'+activeTrades.map(renderTrade).join('')+'</tbody></table>';
        }
        if (closedTrades.length) {
          html += '<div class="px-5 py-2 border-t border-zinc-800 bg-zinc-900/30"><span class="text-[11px] font-semibold uppercase tracking-wider text-zinc-600">History</span></div>';
          html += '<table class="w-full"><thead>'+headerRow+'</thead><tbody>'+closedTrades.map(renderTrade).join('')+'</tbody></table>';
        }
        if (!trades.length) {
          html = '<div class="py-12 text-center text-zinc-600 text-sm">No trades yet</div>';
        }

        $('loading').style.display = 'none';
        $('trades-container').style.display = 'block';
        $('trades-container').innerHTML = html;
        $('trade-count').textContent = stats.active + ' active · ' + stats.total + ' total';

        // Telegram messages feed
        if (messages && messages.length) {
          $('msg-count').textContent = messages.length + ' messages';
          $('messages-container').innerHTML = messages.map(m => {
            const time = m.received_at ? m.received_at.slice(5,19).replace('T',' ') : '';
            const editTag = m.is_edit ? '<span class="badge" style="background:rgba(234,179,8,0.15);color:#facc15;">EDIT</span> ' : '';
            const signalTag = m.is_signal ? '<span class="badge badge-active">SIGNAL</span> ' : '';
            const blockTag = m.block_reason ? '<span class="badge badge-error">'+esc(m.block_reason)+'</span> ' : '';
            const textPreview = esc(m.text.slice(0, 200)) + (m.text.length > 200 ? '...' : '');
            return '<div class="px-4 py-3 border-b border-zinc-800/50 hover:bg-white/[0.02]">'
              + '<div class="flex items-center gap-2 mb-1">'
              + '<span class="text-[11px] text-zinc-500 mono">'+time+'</span> '
              + '<span class="text-[11px] text-zinc-600">#'+m.message_id+'</span> '
              + editTag + signalTag + blockTag
              + '</div>'
              + '<p class="text-xs text-zinc-400 leading-relaxed whitespace-pre-line">'+textPreview+'</p>'
              + '</div>';
          }).join('');
        } else {
          $('messages-container').innerHTML = '<div class="py-8 text-center text-zinc-600 text-sm">No messages received yet</div>';
        }

        $('ts').textContent = stats.timestamp || '';
      } catch(e) {
        $('loading').innerHTML = '<span class="text-red-400">'+e.message+'</span>';
      }
    }

    load();
    setInterval(load, 15000);
  </script>
</body>
</html>"""

"""
Dashboard — live view of trades and wallet.
"""

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["Dashboard"])


@router.get("/dashboard/data", response_class=JSONResponse)
async def dashboard_data():
    from .routes import get_trade_manager
    from ..database.repository import get_db_session, TradeRepository

    trades_list = []
    stats = {"active": 0, "closed": 0, "total": 0, "usdc_balance": None, "eth_balance": None}

    try:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            trades = await repo.get_recent_trades(limit=100)
            for t in trades:
                trades_list.append(t.to_dict())
                stats["total"] += 1
                if t.status.value in ("active", "opening", "pending"):
                    stats["active"] += 1
                elif t.status.value == "closed":
                    stats["closed"] += 1
    except Exception:
        pass

    try:
        tm = get_trade_manager()
        if tm.client:
            stats["usdc_balance"] = round(await tm.client.get_usdc_balance(), 2)
            stats["eth_balance"] = round(float(await tm.client.get_eth_balance()), 6)
    except Exception:
        pass

    stats["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"trades": trades_list, "stats": stats}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return _html()


def _html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Avantis Bot — Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: { sans: ['Outfit','sans-serif'], mono: ['JetBrains Mono','monospace'] },
          colors: { bg:'#0a0b0f', surface:'#12141c', border:'#1e2230', muted:'#64748b', green:'#22c55e', red:'#ef4444', accent:'#3b82f6' }
        }
      }
    }
  </script>
  <style>
    body { font-family:'Outfit',sans-serif; background:#0a0b0f; }
    .card { background:linear-gradient(145deg,#12141c,#0f1118); border:1px solid #1e2230; border-radius:1rem; padding:1.25rem; }
  </style>
</head>
<body class="text-slate-100 min-h-screen antialiased">
  <div class="max-w-6xl mx-auto px-4 py-8">
    <header class="mb-6">
      <h1 class="text-2xl font-bold text-white">Telegram → Avantis Bot</h1>
      <p class="text-slate-500 text-sm mt-1">Live dashboard · Base chain perpetual futures</p>
    </header>

    <div id="stats" class="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8"></div>

    <div class="card overflow-hidden">
      <h3 class="text-sm font-semibold text-slate-400 mb-4">Recent Trades</h3>
      <div class="overflow-x-auto">
        <table id="trades" class="w-full text-sm font-mono" style="display:none">
          <thead class="text-slate-500 text-xs uppercase"><tr id="thead"></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
        <p id="loading" class="text-center text-slate-500 py-8">Loading...</p>
      </div>
    </div>

    <p id="ts" class="text-xs text-slate-600 mt-4 font-mono"></p>
  </div>

  <script>
    const COLS = ['Symbol','Direction','Leverage','Entry','TP','SL','Size ($)','Status','TX','Opened','Closed'];

    function statCard(label, value, cls='text-white') {
      return '<div class="card"><p class="text-xs uppercase text-slate-500 mb-1">'+label+'</p><p class="font-mono text-lg font-semibold '+cls+'">'+value+'</p></div>';
    }

    function esc(s) { if(s==null) return ''; let d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

    async function load() {
      try {
        const r = await fetch('/dashboard/data');
        const {trades, stats} = await r.json();

        let cards = '';
        if (stats.usdc_balance != null) cards += statCard('USDC Balance', '$'+stats.usdc_balance.toLocaleString(), 'text-blue-400');
        if (stats.eth_balance != null) cards += statCard('ETH Balance', stats.eth_balance+' ETH', 'text-blue-400');
        cards += statCard('Active Trades', stats.active);
        cards += statCard('Closed Trades', stats.closed);
        cards += statCard('Total Trades', stats.total);
        document.getElementById('stats').innerHTML = cards;

        const thead = document.getElementById('thead');
        thead.innerHTML = COLS.map(c => '<th class="text-left py-2 px-3">'+c+'</th>').join('');

        const tbody = document.getElementById('tbody');
        tbody.innerHTML = trades.map((t,i) => {
          const targets = Array.isArray(t.targets) ? t.targets : [];
          const tp = targets.length ? targets[0] : '—';
          const dir = t.direction || '';
          const dirCls = dir === 'long' ? 'text-green-400' : 'text-red-400';
          const statusCls = t.status === 'active' ? 'text-green-400' : t.status === 'error' ? 'text-red-400' : 'text-slate-400';
          const txShort = t.tx_hash ? t.tx_hash.slice(0,10)+'...' : '—';
          const txLink = t.tx_hash && t.tx_hash !== 'DRY_RUN' ? '<a href="https://basescan.org/tx/'+t.tx_hash+'" target="_blank" class="text-blue-400 hover:underline">'+txShort+'</a>' : txShort;
          const cells = [
            esc(t.symbol), '<span class="'+dirCls+'">'+esc(dir)+'</span>', t.leverage+'x',
            t.entry_price, tp, t.stop_loss, '$'+t.position_size_usd,
            '<span class="'+statusCls+'">'+esc(t.status)+'</span>',
            txLink,
            t.opened_at ? t.opened_at.slice(0,19) : '—',
            t.closed_at ? t.closed_at.slice(0,19) : '—',
          ];
          const bg = i%2===0 ? 'bg-white/[0.02]' : '';
          return '<tr class="'+bg+'">'+cells.map(c => '<td class="py-2 px-3 border-b border-[#1e2230]">'+c+'</td>').join('')+'</tr>';
        }).join('');

        document.getElementById('loading').style.display = 'none';
        document.getElementById('trades').style.display = 'table';
        document.getElementById('ts').textContent = 'Updated: ' + (stats.timestamp || '—');
      } catch(e) {
        document.getElementById('loading').innerHTML = '<span class="text-red-400">Error: '+e.message+'</span>';
      }
    }

    load();
    setInterval(load, 30000);
  </script>
</body>
</html>"""

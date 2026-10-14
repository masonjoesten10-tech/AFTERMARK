#!/usr/bin/env python3
"""AFTERMARK local research server. Standard library only; no trading/signing capability."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path
import json, sqlite3, time, urllib.request, urllib.error, re, os

ROOT = Path(__file__).resolve().parent
# On Render, mount a persistent disk at /var/data and set AFTERMARK_DB_PATH=/var/data/aftermark.db.
DB = Path(os.environ.get('AFTERMARK_DB_PATH', str(ROOT / 'aftermark.db'))).resolve()
DB.parent.mkdir(parents=True, exist_ok=True)
DEX = 'https://api.dexscreener.com'
GECKO = 'https://api.geckoterminal.com/api/v2'

def db_conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS snapshots (
      id INTEGER PRIMARY KEY, captured_at INTEGER NOT NULL, chain TEXT NOT NULL,
      mint TEXT NOT NULL, pair_address TEXT, symbol TEXT, name TEXT, price_usd REAL,
      market_cap REAL, fdv REAL, liquidity_usd REAL, volume_24h REAL,
      change_24h REAL, buys_24h INTEGER, sells_24h INTEGER, source TEXT NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_snap_mint_time ON snapshots(mint,captured_at)')
    c.commit()
    return c

def fetch_json(url, timeout=18):
    req = urllib.request.Request(url, headers={'accept':'application/json','user-agent':'AFTERMARK-research/0.2'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def best_pair(mint):
    'Resolve a pair by exact mint address and orient it around the requested token.'
    if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', mint):
        raise ValueError('Enter a valid-looking Solana mint address. Name search is available in the browser, but the saved ledger requires an exact mint.')
    data = fetch_json(f'{DEX}/token-pairs/v1/solana/{mint}')
    pairs = [p for p in (data if isinstance(data, list) else []) if p.get('chainId') == 'solana']
    if not pairs:
        data = fetch_json(f'{DEX}/latest/dex/search?q={mint}')
        pairs = [p for p in (data.get('pairs') or []) if p.get('chainId') == 'solana' and (p.get('baseToken',{}).get('address') == mint or p.get('quoteToken',{}).get('address') == mint)]
    # Exact mint identity is mandatory. Prefer a pair where the requested mint is the base token.
    exact = [p for p in pairs if (p.get('baseToken') or {}).get('address') == mint]
    if exact:
        pairs = exact
    else:
        pairs = [p for p in pairs if (p.get('quoteToken') or {}).get('address') == mint]
    if not pairs:
        raise ValueError('No Solana pair found for that exact mint. Confirm the address and that a pool exists.')
    pairs.sort(key=lambda p: float((p.get('liquidity') or {}).get('usd') or 0), reverse=True)
    p = dict(pairs[0])
    if (p.get('baseToken') or {}).get('address') != mint:
        # Orient the record around the requested token, instead of silently saving the pair's other token.
        old_base, old_quote = p.get('baseToken') or {}, p.get('quoteToken') or {}
        p['baseToken'], p['quoteToken'] = old_quote, old_base
        price = _num(p.get('priceUsd'))
        p['priceUsd'] = (1.0 / price) if price and price > 0 else None
        # Pair-level market cap/FDV belongs to the original base asset and cannot safely be reused.
        p['marketCap'] = None
        p['fdv'] = None
        change = _num((p.get('priceChange') or {}).get('h24'))
        if change is not None and (1.0 + change / 100.0) != 0:
            p['priceChange'] = dict(p.get('priceChange') or {})
            p['priceChange']['h24'] = ((1.0 / (1.0 + change / 100.0)) - 1.0) * 100.0
    if (p.get('baseToken') or {}).get('address') != mint:
        raise ValueError('Mint identity validation failed; no snapshot was saved.')
    return p

def write_snapshot(p, requested_mint=None):
    b = p.get('baseToken') or {}; mint = b.get('address') or ''
    if not mint or (requested_mint and mint != requested_mint):
        raise ValueError('Returned token mint did not match the requested mint. Snapshot rejected.')
    txn = (p.get('txns') or {}).get('h24') or {}
    with db_conn() as c:
        c.execute('''INSERT INTO snapshots(captured_at,chain,mint,pair_address,symbol,name,price_usd,market_cap,fdv,liquidity_usd,volume_24h,change_24h,buys_24h,sells_24h,source)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (int(time.time()),'solana',mint,p.get('pairAddress'),b.get('symbol'),b.get('name'),_num(p.get('priceUsd')),_num(p.get('marketCap')),_num(p.get('fdv')),_num((p.get('liquidity') or {}).get('usd')),_num((p.get('volume') or {}).get('h24')),_num((p.get('priceChange') or {}).get('h24')),_int(txn.get('buys')),_int(txn.get('sells')),'DexScreener public API'))
        return c.execute('SELECT * FROM snapshots WHERE id=last_insert_rowid()').fetchone()

def _num(v):
    try: return float(v) if v is not None else None
    except (ValueError, TypeError): return None
def _int(v):
    try: return int(v) if v is not None else None
    except (ValueError, TypeError): return None
def rowdict(r): return dict(r) if r else None

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(ROOT),**kw)
    def log_message(self, fmt, *args): print('[AFTERMARK]', fmt % args)
    def send_json(self, obj, status=200):
        raw=json.dumps(obj, separators=(',',':')).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Access-Control-Allow-Origin','*'); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        u=urlparse(self.path); path=u.path; q=parse_qs(u.query)
        if path == '/':
            self.send_response(302); self.send_header('Location', '/aftermark.html'); self.end_headers(); return
        if path in ('/server.py', '/aftermark.db', '/README.md', '/Dockerfile', '/render.yaml', '/.gitignore') or path.endswith(('.db', '.pyc')):
            return self.send_error(404, 'Not found')
        try:
            if path == '/api/health':
                with db_conn() as c: count=c.execute('SELECT COUNT(*) n FROM snapshots').fetchone()['n']
                return self.send_json({'ok':True,'service':'AFTERMARK research server','snapshots':count,'trading_enabled':False})
            if path == '/api/collect':
                raw=(q.get('mints') or [''])[0]
                # Batch collection is intentionally bounded to protect public API quotas.
                mints=[]
                for item in re.split(r'[\s,;]+', raw.strip()):
                    if item and item not in mints: mints.append(item)
                if not mints: return self.send_json({'error':'Supply mints=ADDRESS1,ADDRESS2,...'},400)
                if len(mints)>15: return self.send_json({'error':'Maximum 15 unique mints per batch. Run smaller batches to respect public API limits.'},400)
                invalid=[m for m in mints if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',m)]
                if invalid: return self.send_json({'error':'One or more entries do not look like Solana mint addresses. Remove names and check for typos.','invalid':invalid},400)
                results=[]
                for i,mint in enumerate(mints):
                    try:
                        pair=best_pair(mint)
                        row=write_snapshot(pair, requested_mint=mint)
                        results.append({'mint':mint,'ok':True,'snapshot_id':row['id'],'captured_at':row['captured_at'],'symbol':row['symbol'],'name':row['name'],'price_usd':row['price_usd'],'liquidity_usd':row['liquidity_usd'],'volume_24h':row['volume_24h'],'pair_address':row['pair_address']})
                    except Exception as e:
                        results.append({'mint':mint,'ok':False,'error':str(e)[:240]})
                    if i < len(mints)-1: time.sleep(0.25)
                with db_conn() as c: total=c.execute('SELECT COUNT(*) n FROM snapshots').fetchone()['n']
                return self.send_json({'requested':len(mints),'saved':sum(1 for x in results if x['ok']),'failed':sum(1 for x in results if not x['ok']),'total_snapshots':total,'results':results,'notice':'Each successful row is a current observation only. This endpoint does not reconstruct prior market states or create validated analogues.'})
            if path == '/api/analyze':
                mint=(q.get('mint') or [''])[0].strip()
                if not mint or len(mint)>100: return self.send_json({'error':'Supply a token mint address.'},400)
                p=best_pair(mint); row=write_snapshot(p, requested_mint=mint)
                return self.send_json({'pair':p,'snapshot':rowdict(row),'notice':'One timestamped snapshot saved locally. This is not historical trajectory data yet.'})
            if path == '/api/snapshots':
                mint=(q.get('mint') or [''])[0].strip()
                if not mint: return self.send_json({'error':'Supply mint=...'},400)
                limit=max(1,min(500,int((q.get('limit') or ['100'])[0])))
                with db_conn() as c: rows=c.execute('SELECT * FROM snapshots WHERE mint=? ORDER BY captured_at DESC LIMIT ?', (mint,limit)).fetchall()
                return self.send_json({'mint':mint,'count':len(rows),'snapshots':[rowdict(r) for r in reversed(rows)],'notice':'Snapshots only accumulate from the time this server is running; no historical values are fabricated.'})
            if path == '/api/pools':
                mint=(q.get('mint') or [''])[0].strip()
                if not mint: return self.send_json({'error':'Supply mint=...'},400)
                data=fetch_json(f'{DEX}/token-pairs/v1/solana/{mint}')
                pools=data if isinstance(data,list) else []
                pools.sort(key=lambda p:float((p.get('liquidity') or {}).get('usd') or 0),reverse=True)
                return self.send_json({'mint':mint,'pools':pools[:20],'source':'DexScreener public API'})
            if path == '/api/ohlcv':
                pool=(q.get('pool') or [''])[0].strip()
                timeframe=(q.get('timeframe') or ['hour'])[0]
                limit=max(1,min(1000,int((q.get('limit') or ['200'])[0])))
                if not pool or len(pool)>100: return self.send_json({'error':'Supply a pool address.'},400)
                if timeframe not in ('minute','hour','day'): return self.send_json({'error':'timeframe must be minute, hour, or day'},400)
                url=f'{GECKO}/networks/solana/pools/{pool}/ohlcv/{timeframe}?aggregate=1&limit={limit}&currency=usd&token=base'
                try: data=fetch_json(url)
                except urllib.error.HTTPError as e:
                    return self.send_json({'error':f'GeckoTerminal returned HTTP {e.code}. The pool may not be indexed or public API limits may apply.','source':'GeckoTerminal','url':url},502)
                attrs=(((data.get('data') or {}).get('attributes') or {}))
                rows=attrs.get('ohlcv_list') or []
                return self.send_json({'pool':pool,'timeframe':timeframe,'candles':[{'timestamp':r[0],'open':r[1],'high':r[2],'low':r[3],'close':r[4],'volume':r[5]} for r in sorted(rows,key=lambda x:x[0]) if isinstance(r,list) and len(r)>=6], 'source':'GeckoTerminal public API','notice':'Historical candles are pool-specific. They are not proof of token-wide liquidity or future performance.'})
            if path == '/api/analogs':
                mint=(q.get('mint') or [''])[0].strip()
                with db_conn() as c:
                    rows=c.execute('SELECT * FROM snapshots WHERE mint=? ORDER BY captured_at', (mint,)).fetchall() if mint else []
                return self.send_json({'enabled':False,'reason':'Validated cross-token analogues require a sufficiently large, timestamped launch cohort and out-of-sample testing. This build only stores real snapshots and retrieves historical pool candles.','own_snapshot_count':len(rows),'analogue_results':[]})
            if path in ('/aftermark.html', '/index.html'):
                try:
                    raw=(ROOT / 'aftermark.html').read_bytes()
                    self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(raw); return
                except OSError:
                    return self.send_error(404, 'Website file not found')
            return self.send_error(404, 'Not found')
        except urllib.error.HTTPError as e:
            return self.send_json({'error':f'Upstream data source returned HTTP {e.code}. Try again later.','source':'public market data API'},502)
        except (urllib.error.URLError, TimeoutError) as e:
            return self.send_json({'error':'Could not reach the public market data source. Check internet access and retry.','detail':str(e)},502)
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json({'error':str(e)},400)
        except Exception as e:
            return self.send_json({'error':'Unexpected server error.','detail':str(e)},500)

if __name__=='__main__':
    with db_conn() as c: pass
    host=os.environ.get('HOST','127.0.0.1')
    port=int(os.environ.get('PORT','8765'))
    print(f'AFTERMARK research server: http://{host}:{port}/')
    print(f'API health: http://{host}:{port}/api/health')
    print('No wallet connection, private keys, signing, or trading functionality.')
    ThreadingHTTPServer((host,port),Handler).serve_forever()

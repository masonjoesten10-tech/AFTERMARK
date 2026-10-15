   """Bounded retries for transient failures; respect Retry-After on HTTP 429."""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={'accept': 'application/json', 'user-agent': USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = None
                value = exc.headers.get('Retry-After')
                if value:
                    try:
                        wait = max(1.0, float(value))
                    except ValueError:
                        try:
                            wait = max(1.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
                        except Exception:
                            pass
                if wait is None:
                    wait = 2 ** attempt
                # Never hold a public request for a long time if the provider asks us to wait.
                if attempt < retries and wait <= 8:
                    time.sleep(wait)
                    continue
            elif exc.code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError('Market data request failed')


def _num(v):
    try: return float(v) if v is not None else None
    except (ValueError, TypeError): return None


def _int(v):
    try: return int(v) if v is not None else None
    except (ValueError, TypeError): return None


def _valid_mint(mint):
    return bool(re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', mint or ''))


def rowdict(row):
    return dict(row) if row else None


def best_pair(mint):
    """Resolve exact Solana mint, preferring the most liquid pair; briefly cache success."""
    if not _valid_mint(mint):
        raise ValueError('Enter a valid-looking Solana mint address. The ledger requires an exact mint.')
    now = time.time()
    with _cache_lock:
        cached = _pair_cache.get(mint)
        if cached and now - cached[0] <= CACHE_SECONDS:
            return dict(cached[1])
        if cached:
            _pair_cache.pop(mint, None)

    data = fetch_json(f'{DEX}/token-pairs/v1/solana/{mint}')
    pairs = [p for p in (data if isinstance(data, list) else []) if p.get('chainId') == 'solana']
    if not pairs:
        data = fetch_json(f'{DEX}/latest/dex/search?q={mint}')
        pairs = [p for p in (data.get('pairs') or []) if p.get('chainId') == 'solana' and
                 ((p.get('baseToken') or {}).get('address') == mint or (p.get('quoteToken') or {}).get('address') == mint)]
    base_pairs = [p for p in pairs if (p.get('baseToken') or {}).get('address') == mint]
    pairs = base_pairs or [p for p in pairs if (p.get('quoteToken') or {}).get('address') == mint]
    if not pairs:
        raise ValueError('No Solana pair found for that exact mint. Confirm the address and that a pool exists.')
    pairs.sort(key=lambda p: _num((p.get('liquidity') or {}).get('usd')) or 0, reverse=True)
    p = dict(pairs[0])
    if (p.get('baseToken') or {}).get('address') != mint:
        old_base, old_quote = p.get('baseToken') or {}, p.get('quoteToken') or {}
        p['baseToken'], p['quoteToken'] = old_quote, old_base
        price = _num(p.get('priceUsd'))
        p['priceUsd'] = (1.0 / price) if price and price > 0 else None
        p['marketCap'] = None
        p['fdv'] = None
        change = _num((p.get('priceChange') or {}).get('h24'))
        if change is not None and (1.0 + change / 100.0) != 0:
            p['priceChange'] = dict(p.get('priceChange') or {})
            p['priceChange']['h24'] = ((1.0 / (1.0 + change / 100.0)) - 1.0) * 100.0
    if (p.get('baseToken') or {}).get('address') != mint:
        raise ValueError('Mint identity validation failed; snapshot rejected.')
    with _cache_lock:
        _pair_cache[mint] = (time.time(), dict(p))
        if len(_pair_cache) > 250:
            for key, _ in sorted(_pair_cache.items(), key=lambda item: item[1][0])[:50]:
                _pair_cache.pop(key, None)
    return p


def write_snapshot(p, requested_mint=None):
    b = p.get('baseToken') or {}
    mint = b.get('address') or ''
    if not mint or (requested_mint and mint != requested_mint):
        raise ValueError('Returned token mint did not match requested mint. Snapshot rejected.')
    txn = (p.get('txns') or {}).get('h24') or {}
    with db_conn() as c:
        cur = c.execute('''INSERT INTO snapshots(captured_at,chain,mint,pair_address,symbol,name,price_usd,market_cap,fdv,liquidity_usd,volume_24h,change_24h,buys_24h,sells_24h,source)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (int(time.time()), 'solana', mint, p.get('pairAddress'), b.get('symbol'), b.get('name'),
          _num(p.get('priceUsd')), _num(p.get('marketCap')), _num(p.get('fdv')), _num((p.get('liquidity') or {}).get('usd')),
          _num((p.get('volume') or {}).get('h24')), _num((p.get('priceChange') or {}).get('h24')), _int(txn.get('buys')),
          _int(txn.get('sells')), 'DexScreener public API'))
        return c.execute('SELECT * FROM snapshots WHERE id=?', (cur.lastrowid,)).fetchone()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print('[AFTERMARK]', fmt % args, flush=True)

    def send_json(self, obj, status=200, headers=None):
        raw = json.dumps(obj, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def upstream_error(self, exc):
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            return self.send_json({'error': 'The public market-data provider is rate-limiting requests. Wait a few minutes before retrying. No snapshot was saved for this failed request.', 'source': 'DexScreener public API', 'retryable': True}, 503, {'Retry-After': '60'})
        if isinstance(exc, urllib.error.HTTPError):
            return self.send_json({'error': f'Upstream data source returned HTTP {exc.code}. Try again later.', 'source': 'public market data API', 'retryable': exc.code >= 500}, 502)
        return self.send_json({'error': 'Could not reach the public market-data source. Check connectivity and retry.', 'source': 'public market data API', 'retryable': True}, 502)

    def do_GET(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if path == '/':
            self.send_response(302); self.send_header('Location', '/aftermark.html'); self.end_headers(); return
        if path in ('/server.py', '/aftermark.db', '/README.md', '/Dockerfile', '/render.yaml', '/.gitignore') or path.endswith(('.db', '.pyc')):
            return self.send_error(404, 'Not found')
        try:
            if path == '/api/health':
                with db_conn() as c: count = c.execute('SELECT COUNT(*) n FROM snapshots').fetchone()['n']
                return self.send_json({'ok': True, 'service': 'AFTERMARK research server', 'snapshots': count, 'trading_enabled': False, 'pair_cache_seconds': CACHE_SECONDS})
            if path == '/api/collect':
                raw = (q.get('mints') or [''])[0]
                mints = list(dict.fromkeys(item for item in re.split(r'[\s,;]+', raw.strip()) if item))
                if not mints: return self.send_json({'error': 'Supply mints=ADDRESS1,ADDRESS2,...'}, 400)
                if len(mints) > 15: return self.send_json({'error': 'Maximum 15 unique mints per batch.'}, 400)
                invalid = [m for m in mints if not _valid_mint(m)]
                if invalid: return self.send_json({'error': 'One or more entries do not look like Solana mint addresses.', 'invalid': invalid}, 400)
                results = []
                for i, mint in enumerate(mints):
                    try:
                        pair = best_pair(mint); row = write_snapshot(pair, requested_mint=mint)
                        results.append({'mint': mint, 'ok': True, 'snapshot_id': row['id'], 'captured_at': row['captured_at'], 'symbol': row['symbol'], 'name': row['name'], 'price_usd': row['price_usd'], 'liquidity_usd': row['liquidity_usd'], 'volume_24h': row['volume_24h'], 'pair_address': row['pair_address']})
                    except Exception as e:
                        results.append({'mint': mint, 'ok': False, 'error': 'Provider rate limit (HTTP 429); wait before retrying.' if isinstance(e, urllib.error.HTTPError) and e.code == 429 else str(e)[:240]})
                    if i < len(mints) - 1: time.sleep(0.5)
                with db_conn() as c: total = c.execute('SELECT COUNT(*) n FROM snapshots').fetchone()['n']
                return self.send_json({'requested': len(mints), 'saved': sum(1 for x in results if x['ok']), 'failed': sum(1 for x in results if not x['ok']), 'total_snapshots': total, 'results': results, 'notice': 'Each successful row is a current observation only; this does not reconstruct prior market states.'})
            if path == '/api/analyze':
                mint = (q.get('mint') or [''])[0].strip()
                if not _valid_mint(mint): return self.send_json({'error': 'Supply a valid Solana token mint address.'}, 400)
                p = best_pair(mint); row = write_snapshot(p, requested_mint=mint)
                return self.send_json({'pair': p, 'snapshot': rowdict(row), 'notice': 'One timestamped snapshot saved. This is not historical trajectory data yet.'})
            if path == '/api/snapshots':
                mint = (q.get('mint') or [''])[0].strip()
                if not _valid_mint(mint): return self.send_json({'error': 'Supply mint=... with a valid Solana mint address.'}, 400)
                limit = max(1, min(500, int((q.get('limit') or ['100'])[0])))
                with db_conn() as c: rows = c.execute('SELECT * FROM snapshots WHERE mint=? ORDER BY captured_at DESC LIMIT ?', (mint, limit)).fetchall()
                return self.send_json({'mint': mint, 'count': len(rows), 'snapshots': [rowdict(r) for r in reversed(rows)], 'notice': 'Snapshots accumulate only from the time they are collected; no historical values are fabricated.'})
            if path == '/api/pools':
                mint = (q.get('mint') or [''])[0].strip()
                if not _valid_mint(mint): return self.send_json({'error': 'Supply a valid Solana mint address.'}, 400)
                pools = fetch_json(f'{DEX}/token-pairs/v1/solana/{mint}')
                pools = pools if isinstance(pools, list) else []
                pools.sort(key=lambda p: _num((p.get('liquidity') or {}).get('usd')) or 0, reverse=True)
                return self.send_json({'mint': mint, 'pools': pools[:20], 'source': 'DexScreener public API'})
            if path == '/api/ohlcv':
                pool = (q.get('pool') or [''])[0].strip(); timeframe = (q.get('timeframe') or ['hour'])[0]
                limit = max(1, min(1000, int((q.get('limit') or ['200'])[0])))
                if not pool or len(pool) > 100: return self.send_json({'error': 'Supply a pool address.'}, 400)
                if timeframe not in ('minute', 'hour', 'day'): return self.send_json({'error': 'timeframe must be minute, hour, or day'}, 400)
                url = f'{GECKO}/networks/solana/pools/{pool}/ohlcv/{timeframe}?aggregate=1&limit={limit}&currency=usd&token=base'
                data = fetch_json(url)
                attrs = (((data.get('data') or {}).get('attributes') or {})); rows = attrs.get('ohlcv_list') or []
                candles = [{'timestamp': r[0], 'open': r[1], 'high': r[2], 'low': r[3], 'close': r[4], 'volume': r[5]} for r in sorted(rows, key=lambda x: x[0]) if isinstance(r, list) and len(r) >= 6]
                return self.send_json({'pool': pool, 'timeframe': timeframe, 'candles': candles, 'source': 'GeckoTerminal public API', 'notice': 'Historical candles are pool-specific and do not predict future performance.'})
            if path == '/api/analogs':
                mint = (q.get('mint') or [''])[0].strip()
                with db_conn() as c: rows = c.execute('SELECT * FROM snapshots WHERE mint=? ORDER BY captured_at', (mint,)).fetchall() if mint else []
                return self.send_json({'enabled': False, 'reason': 'Validated cross-token analogues require a sufficiently large timestamped launch cohort and out-of-sample testing.', 'own_snapshot_count': len(rows), 'analogue_results': []})
            if path in ('/aftermark.html', '/index.html'):
                try: raw = (ROOT / 'aftermark.html').read_bytes()
                except OSError: return self.send_error(404, 'Website file not found')
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(raw))); self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers(); self.wfile.write(raw); return
            return self.send_error(404, 'Not found')
        except urllib.error.HTTPError as e:
            return self.upstream_error(e)
        except (urllib.error.URLError, TimeoutError) as e:
            return self.upstream_error(e)
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json({'error': str(e)}, 400)
        except Exception as e:
            print(f'[AFTERMARK] Unexpected error: {e!r}', flush=True)
            return self.send_json({'error': 'Unexpected server error. Check Render logs.'}, 500)


if __name__ == '__main__':
    with db_conn(): pass
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8765'))
    print(f'AFTERMARK research server: http://{host}:{port}/', flush=True)
    print(f'API health: http://{host}:{port}/api/health', flush=True)
    print('No wallet connection, private keys, signing, or trading functionality.', flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


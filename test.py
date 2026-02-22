import time
import requests
import json
from py_clob_client.client import ClobClient
from datetime import datetime, timezone

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
SHARES_PER_TRADE = 5
BUY_MIN = 0.31
BUY_MAX = 0.35
POLL_INTERVAL_SEC = 0.5
REFETCH_MARKET_EVERY_SEC = 0.5

HOST_CLOB = "https://clob.polymarket.com"
CHAIN_ID = 137

LOG_FILE = "trades_log_5m.txt"

# State
virtual_balance = 50.0
virtual_positions = {}
last_slug = None
last_market_refetch = 0
traded_in_market = set()                # token IDs already bought in current market

def log_trade(message):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    line = f"[{ts}] {message}\n"
    print(line.strip())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)

def guess_current_5m_slug(offset_minutes=2):
    """Guess slug for current or very near 5-min window"""
    now = int(time.time())
    interval = 5 * 60
    adjusted = now + (offset_minutes * 60)
    start_ts = (adjusted // interval) * interval
    return f"btc-updown-5m-{start_ts}"

def fetch_market_by_slug(slug):
    url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
    try:
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        print(f"Fetch error for {slug}: {e}")
        return None

def fallback_fetch_latest_btc_5m():
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "limit": 30,
        "active": "true",
        "closed": "false",
        "order": "endDate",
        "ascending": "true"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
        candidates = [
            m for m in markets
            if m.get("slug", "").startswith("btc-updown-5m-")
            and m.get("active", False)
        ]
        if candidates:
            return min(candidates, key=lambda m: m.get("endDate") or "9999-12-31T23:59:59Z")
        return None
    except Exception as e:
        print(f"Fallback fetch failed: {e}")
        return None

# ────────────────────────────────────────────────
print("BTC 5-min Arbitrage Paper Trading Bot")
print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

# Initial market fetch
slug = guess_current_5m_slug(offset_minutes=3)
market = fetch_market_by_slug(slug)

if not market or not market.get("active", False):
    print("Initial guess not active → fallback to latest BTC 5m market")
    market = fallback_fetch_latest_btc_5m()

if not market:
    print("Could not find any active BTC 5-min Up/Down market. Exiting.")
    exit(1)

last_slug = market["slug"]

# ── Parse tokens ──
token_ids = [str(tid).strip('"') for tid in json.loads(market.get("clobTokenIds", "[]"))]
outcomes = json.loads(market.get("outcomes", "[]"))

if len(token_ids) != 2:
    print("Could not parse exactly 2 token IDs. Exiting.")
    exit(1)

up_token_id   = token_ids[0]
down_token_id = token_ids[1]
up_outcome    = outcomes[0].upper() if outcomes else "UP"
down_outcome  = outcomes[1].upper() if outcomes else "DOWN"

token_info = {up_token_id: up_outcome, down_token_id: down_outcome}
has_position = {tid: False for tid in token_info}

client = ClobClient(HOST_CLOB, chain_id=CHAIN_ID)

print(f"Initial market: {market.get('question', 'Unknown')}")
print(f"Slug: {last_slug}")
print(f"UP:   {up_outcome}  {up_token_id[:12]}...")
print(f"DOWN: {down_outcome}  {down_token_id[:12]}...")
print("-" * 60 + "\n")

with open(LOG_FILE, "a", encoding="utf-8") as f:
    f.write(f"\n=== Bot session started {datetime.now(timezone.utc)} ===\n")

# ────────────────────────────────────────────────
while True:
    current_time = time.time()

    # Refetch market periodically
    if current_time - last_market_refetch > REFETCH_MARKET_EVERY_SEC:
        last_market_refetch = current_time
        new_slug = guess_current_5m_slug(offset_minutes=0)
        new_market = fetch_market_by_slug(new_slug)

        if not new_market or not new_market.get("active"):
            new_market = fallback_fetch_latest_btc_5m()

        if new_market and new_market.get("slug") != last_slug:
            print("\n" + "="*70)
            print("MARKET CHANGED (new 5-min interval) - Cleaning State")
            print(f"Old: {market.get('question')}  |  {last_slug}")
            print(f"New: {new_market.get('question')}  |  {new_market['slug']}")
            print("="*70 + "\n")

            # Force exit old positions
            tids_to_exit = list(virtual_positions.keys())
            for tid in tids_to_exit:
                pos = virtual_positions[tid]
                shares = pos['shares']
                exit_price = 0.5
                try:
                    m_raw = client.get_midpoint(tid)
                    if isinstance(m_raw, dict) and 'mid' in m_raw: exit_price = float(m_raw['mid'])
                    elif isinstance(m_raw, (str, int, float)): exit_price = float(m_raw)
                except: pass

                proceeds = shares * exit_price
                virtual_balance += proceeds
                pnl = proceeds - (shares * pos['buy_price'])
                log_trade(f"MARKET CLOSE EXIT: {pos['outcome']} @ {exit_price:.4f} P&L ${pnl:+.2f} Bal ${virtual_balance:.2f}")
                del virtual_positions[tid]

            # RESET state
            market = new_market
            last_slug = market["slug"]
            traded_in_market.clear()

            token_ids = [str(tid).strip('"') for tid in json.loads(market.get("clobTokenIds", "[]"))]
            outcomes = json.loads(market.get("outcomes", "[]"))

            if len(token_ids) == 2:
                up_token_id = token_ids[0]
                down_token_id = token_ids[1]
                up_outcome = outcomes[0].upper() if outcomes else "UP"
                down_outcome = outcomes[1].upper() if outcomes else "DOWN"
                token_info = {up_token_id: up_outcome, down_token_id: down_outcome}
                has_position = {tid: False for tid in token_info}

                log_trade(f"Switched to new market: {market.get('question')} | {last_slug}")
            else:
                print("Warning: new market has invalid token count - keeping old tokens")

    # Show Gamma prices if available
    try:
        gamma_prices = json.loads(market.get("outcomePrices", "[]"))
        if len(gamma_prices) == 2:
            print(f"Gamma: {up_outcome} {float(gamma_prices[0]):.4f} | {down_outcome} {float(gamma_prices[1]):.4f}")
    except:
        pass

    # Cache current mids
    current_mids = {}

    for tid, outcome in token_info.items():
        mid = None
        try:
            mid_raw = client.get_midpoint(tid)
            if isinstance(mid_raw, dict) and 'mid' in mid_raw:
                mid = float(mid_raw['mid'])
            elif isinstance(mid_raw, (str, int, float)):
                mid = float(mid_raw)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] {outcome} midpoint error: {e}")
            continue

        if mid is None or mid <= 0.0001:
            continue

        current_mids[tid] = mid

        print(f"[{time.strftime('%H:%M:%S')}] {outcome:<6} mid: {mid:.4f}")

        pos = virtual_positions.get(tid, {})
        shares = pos.get('shares', 0.0)

        # Entry condition
        if tid not in traded_in_market:
            if len(traded_in_market) == 0:
                if BUY_MIN <= mid <= BUY_MAX:
                    cost = SHARES_PER_TRADE * mid
                    if virtual_balance < cost:
                        print(f"Low balance for {outcome} buy")
                        continue

                    # All checks passed → enter
                    traded_in_market.add(tid)
                    
                    shares_bought = SHARES_PER_TRADE
                    virtual_balance -= cost
                    virtual_positions[tid] = {
                        'shares': shares_bought,
                        'buy_price': mid,
                        'outcome': outcome
                    }
                    has_position[tid] = True
                    
                    msg = f"** BUY ** {outcome} @ {mid:.4f}   {shares_bought:.4f} sh   Cost ${cost:.2f}   Bal ${virtual_balance:.2f}"
                    log_trade(msg)
            else:
                if mid <= BUY_MAX:
                    cost = SHARES_PER_TRADE * mid
                    if virtual_balance < cost:
                        print(f"Low balance for {outcome} buy")
                        continue

                    # All checks passed → enter
                    traded_in_market.add(tid)
                    
                    shares_bought = SHARES_PER_TRADE
                    virtual_balance -= cost
                    virtual_positions[tid] = {
                        'shares': shares_bought,
                        'buy_price': mid,
                        'outcome': outcome
                    }
                    has_position[tid] = True
                    
                    msg = f"** BUY ** {outcome} @ {mid:.4f}   {shares_bought:.4f} sh   Cost ${cost:.2f}   Bal ${virtual_balance:.2f}"
                    log_trade(msg)

    # Status line
    up_s   = virtual_positions.get(up_token_id,   {}).get('shares', 0)
    down_s = virtual_positions.get(down_token_id, {}).get('shares', 0)
    print(f"[{time.strftime('%H:%M:%S')}] Bal ${virtual_balance:.2f}   {up_outcome}: {up_s:.4f}   {down_outcome}: {down_s:.4f}\n")

    time.sleep(POLL_INTERVAL_SEC)

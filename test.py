import time
import requests
import json
from py_clob_client.client import ClobClient
from datetime import datetime, timezone

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
AMOUNT_USDC_PER_TRADE = 1.0
BUY_PRICE = 0.40      # Buy when mid is around this (e.g. cheap Up or Down shares)
SL_PRICE = 0.20
TP_PRICE = 0.70
TOLERANCE = 0.02      # Allow ±1 cent around BUY_PRICE
POLL_INTERVAL_SEC = 1
REFETCH_MARKET_EVERY_SEC = 10

HOST_CLOB = "https://clob.polymarket.com"
CHAIN_ID = 137

LOG_FILE = "trades_log.txt"

# State
virtual_balance = 5.0  # ← start with something realistic; change as you wish
virtual_positions = {}
last_slug = None
last_market_refetch = 0

def log_trade(message):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    line = f"[{ts}] {message}\n"
    print(line.strip())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)

def guess_current_15m_slug(offset_minutes=2):
    """Guess slug for current or very near 15-min window"""
    now = int(time.time())
    interval = 15 * 60  # Changed to 15 minutes
    adjusted = now + (offset_minutes * 60)
    start_ts = (adjusted // interval) * interval
    return f"btc-updown-15m-{start_ts}" # Updated slug prefix

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

def fallback_fetch_latest_btc_15m():
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
            if m.get("slug", "").startswith("btc-updown-15m-") # Updated filter
            and m.get("active", False)
        ]
        if candidates:
            # Soonest ending active market (most likely current/next)
            return min(candidates, key=lambda m: m.get("endDate") or "9999-12-31T23:59:59Z")
        return None
    except Exception as e:
        print(f"Fallback fetch failed: {e}")
        return None

# ────────────────────────────────────────────────
print("BTC 15-min Paper Trading Bot")
print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

# Initial market fetch
slug = guess_current_15m_slug(offset_minutes=3)  # slight future bias helps catch open market
market = fetch_market_by_slug(slug)

if not market or not market.get("active", False):
    print("Initial guess not active → fallback to latest BTC 15m market")
    market = fallback_fetch_latest_btc_15m()

if not market:
    print("Could not find any active BTC 15-min Up/Down market. Exiting.")
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

# Write header to log file if new
with open(LOG_FILE, "a", encoding="utf-8") as f:
    f.write(f"\n=== Bot session started {datetime.now(timezone.utc)} ===\n")

# ────────────────────────────────────────────────
while True:
    current_time = time.time()

    # Refetch market periodically (15-min markets)
    if current_time - last_market_refetch > REFETCH_MARKET_EVERY_SEC:
        last_market_refetch = current_time
        new_slug = guess_current_15m_slug(offset_minutes=0)
        new_market = fetch_market_by_slug(new_slug)

        if not new_market or not new_market.get("active"):
            new_market = fallback_fetch_latest_btc_15m()

        if new_market and new_market.get("slug") != last_slug:
            print("\n" + "="*70)
            print("MARKET CHANGED (new 15-min interval) - Liquidating Old Positions")
            print(f"Old: {market.get('question')}  |  {last_slug}")
            print(f"New: {new_market.get('question')}  |  {new_market['slug']}")
            print("="*70 + "\n")

            # Force Exit any open positions from the old market before switching
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

            market = new_market
            last_slug = market["slug"]

            # Re-parse tokens
            token_ids = [str(tid).strip('"') for tid in json.loads(market.get("clobTokenIds", "[]"))]
            outcomes = json.loads(market.get("outcomes", "[]"))

            if len(token_ids) == 2:
                up_token_id = token_ids[0]
                down_token_id = token_ids[1]
                up_outcome = outcomes[0].upper() if outcomes else "UP"
                down_outcome = outcomes[1].upper() if outcomes else "DOWN"
                token_info = {up_token_id: up_outcome, down_token_id: down_outcome}
                has_position = {tid: False for tid in token_info}  # reset tracking
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

        print(f"[{time.strftime('%H:%M:%S')}] {outcome:<6} mid: {mid:.4f}")

        pos = virtual_positions.get(tid, {})
        shares = pos.get('shares', 0.0)

        # Check exit conditions first (SL/TP)
        if shares > 0:
            if mid <= SL_PRICE or mid >= TP_PRICE:
                exit_type = "SL" if mid <= SL_PRICE else "TP"
                proceeds = shares * mid
                pnl = proceeds - shares * pos['buy_price']
                virtual_balance += proceeds
                msg = f"{exit_type} {outcome} @ {mid:.4f}   P&L ${pnl:+.2f}   Bal ${virtual_balance:.2f}"
                log_trade(msg)
                del virtual_positions[tid]
                has_position[tid] = False

        # Entry condition
        elif abs(mid - BUY_PRICE) <= TOLERANCE and not has_position[tid]:
            if virtual_balance < AMOUNT_USDC_PER_TRADE:
                print(f"Low balance for {outcome} buy")
                continue
            shares_bought = AMOUNT_USDC_PER_TRADE / mid
            virtual_balance -= AMOUNT_USDC_PER_TRADE
            virtual_positions[tid] = {
                'shares': shares_bought,
                'buy_price': mid,
                'outcome': outcome
            }
            has_position[tid] = True
            msg = f"** BUY ** {outcome} @ {mid:.4f}   {shares_bought:.4f} sh   Bal ${virtual_balance:.2f}"
            log_trade(msg)

    # Status line
    up_s   = virtual_positions.get(up_token_id,   {}).get('shares', 0)
    down_s = virtual_positions.get(down_token_id, {}).get('shares', 0)
    print(f"[{time.strftime('%H:%M:%S')}] Bal ${virtual_balance:.2f}   {up_outcome}: {up_s:.4f}   {down_outcome}: {down_s:.4f}\n")

    time.sleep(POLL_INTERVAL_SEC)
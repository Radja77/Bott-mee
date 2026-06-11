import base64
import httpx
import json
import hashlib
import hmac
import time
import re
import asyncio
import os
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, CommandHandler, filters, ContextTypes

# Load config.env dari folder yang sama
_env_path = Path(__file__).parent / "config.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
BYBIT_API_KEY  = os.environ["BYBIT_API_KEY"]
BYBIT_SECRET   = os.environ["BYBIT_SECRET"]

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
BYBIT_BASE = "https://api.bybit.com"

chat_history  = {}
last_analysis = {}
pending_order = {}
active_monitors = {}
time_offset_ms = 0  # selisih waktu HP vs server Bybit

async def sync_time():
    """Sync waktu lokal dengan server Bybit, panggil sekali saat start."""
    global time_offset_ms
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{BYBIT_BASE}/v5/market/time")
        server_ts = int(r.json()["result"]["timeNano"]) // 1_000_000
        local_ts  = int(time.time() * 1000)
        time_offset_ms = server_ts - local_ts
        print(f"Time synced. Offset: {time_offset_ms}ms")
    except Exception as e:
        print(f"Time sync gagal (lanjut tanpa offset): {e}")

def get_timestamp() -> str:
    return str(int(time.time() * 1000) + time_offset_ms)

SYSTEM_PROMPT_CHAT = """Kamu adalah AI trading assistant yang santai, friendly, dan expert di crypto trading.
Kamu bisa ngobrol casual dalam bahasa Indonesia gaul (bro, gw, lu, dll).
Kamu paham analisis teknikal, candlestick, support/resistance, TP/SL, risk management, futures trading.
Jawab singkat, padat, dan to the point."""

VISION_PROMPT = """Kamu adalah trading analyst profesional. Analisa chart trading berikut dengan presisi tinggi.

LANGKAH 1 — BACA INFO DASAR:
- Baca nama pair dari judul chart (contoh: Solana/USD → SOL, Bonfida → FIDA, Pudgy Penguins → PENGU)
- Baca exchange dari judul (BingX, Binance, Bybit, dll)
- Baca timeframe (1h, 30m, 3m, 15m, dll)
- Baca harga saat ini dari label hijau/merah di sisi kanan chart
- PENTING: Angka dengan koma desimal seperti (0,02689) dibaca sebagai 0.02689

LANGKAH 2 — TENTUKAN ARAH (KRITIS):
Baca tanda-tanda berikut secara berurutan:

SINYAL LONG (BUY):
✓ Ada label teks "AREA BUY", "Demand", "fvg", "Support", "Buy Zone" di chart
✓ Ada zona/box BIRU atau TEAL di BAWAH harga saat ini
✓ Harga sudah turun jauh dan mendekati zona support (reversal setup)
✓ Candle terakhir mulai hijau setelah downtrend panjang
✓ Ada panah/kurva dotted menunjuk ke atas

SINYAL SHORT (SELL):
✓ Ada zona/box MERAH di ATAS harga saat ini sebagai entry zone
✓ Harga baru saja pump tajam lalu berbalik turun (rejection dari resistance)
✓ Candle merah dominan, lower high lower low berlanjut
✓ Ada zona merah kecil di atas = resistance/entry SHORT

ATURAN BOX/ZONA:
- Box MERAH KECIL di ATAS = zona SL (untuk LONG) atau zona ENTRY (untuk SHORT)
- Box MERAH BESAR di ATAS harga = zona ENTRY SHORT + SL ada di paling atas box
- Box TEAL/HIJAU di BAWAH harga = zona ENTRY LONG atau zona TP (untuk SHORT)
- Box TEAL/HIJAU BESAR = kalau di bawah entry = multiple TP targets
- Garis horizontal tunggal KUNING/PUTIH = level kunci (support/resistance/SL)
- Label "Demand" atau "fvg" = zona entry LONG yang kuat

LANGKAH 3 — BACA LEVEL BERDASARKAN ARAH:

LONG setup:
- Entry zone = zona hijau/teal/abu-abu/biru TERDEKAT dengan harga saat ini (bisa di bawah atau sedikit di atas)
- zone_low = batas bawah zona entry
- zone_high = batas atas zona entry  
- entry = midpoint zona entry
- TP1, TP2, TP3 = level DI ATAS entry, urut dari terdekat ke terjauh (nilai makin besar)
- SL = garis merah atau batas bawah zona merah PALING BAWAH di chart (nilai terkecil)

SHORT setup:
- Entry zone = zona merah/pink di BAWAH harga saat ini atau di dekat harga
- zone_high = batas atas zona entry (nilai terbesar)
- zone_low = batas bawah zona entry
- entry = midpoint zona entry
- TP1, TP2, TP3 = level DI BAWAH entry, urut dari terdekat ke terjauh (nilai makin kecil)
- SL = batas ATAS zona merah paling atas (nilai terbesar di chart)

LANGKAH 4 — BACA SEMUA ANGKA DI SISI KANAN:
- Baca SEMUA label angka yang ada di sisi kanan chart
- Urutkan dari besar ke kecil
- Cocokkan dengan zona/garis yang ada
- Konversi koma ke titik: (0,02689) → 0.02689, (65.000,0) → 65000.0

VALIDASI WAJIB:
- LONG  → sl < zone_low < entry < zone_high < tp1 < tp2 < tp3
- SHORT → sl > zone_high > entry > zone_low > tp1 > tp2 > tp3
- Jika tidak valid, KOREKSI semua nilai sebelum return JSON"""

def parse_manual_order(text: str) -> dict | None:
    """
    Parse format manual order seperti:
    $FIDA Buy limit 0.02475 - 0.02363 SL 0.02250
    $BTC Short 95000 - 94500 SL 96000 TP 90000
    """
    text = text.strip()
    # Harus ada $ di depan
    if not text.startswith("$"):
        return None

    try:
        # Ambil pair
        m_pair = re.match(r'\$([A-Za-z]+)', text)
        if not m_pair:
            return None
        pair = fix_symbol(m_pair.group(1))

        # Direction: buy/long atau sell/short
        direction = None
        if re.search(r'\b(buy|long)\b', text, re.IGNORECASE):
            direction = "LONG"
        elif re.search(r'\b(sell|short)\b', text, re.IGNORECASE):
            direction = "SHORT"
        if not direction:
            return None

        # Entry zone: dua angka dipisah " - " atau " / "
        m_entry = re.search(r'(\d+\.?\d*)\s*[-/]\s*(\d+\.?\d*)', text)
        if not m_entry:
            return None
        e1, e2 = float(m_entry.group(1)), float(m_entry.group(2))
        entry_high = max(e1, e2)
        entry_low  = min(e1, e2)
        entry      = (entry_high + entry_low) / 2  # midpoint sebagai entry

        # SL
        m_sl = re.search(r'\bSL\s*:?\s*(\d+\.?\d*)', text, re.IGNORECASE)
        sl   = float(m_sl.group(1)) if m_sl else None

        # TP (opsional)
        m_tp = re.search(r'\bTP\s*:?\s*(\d+\.?\d*)', text, re.IGNORECASE)
        tp1  = float(m_tp.group(1)) if m_tp else None

        return {
            "pair"           : pair,
            "direction"      : direction,
            "entry"          : entry,
            "entry_zone_low" : entry_low,
            "entry_zone_high": entry_high,
            "tp1"            : tp1,
            "tp2"            : tp1,
            "tp3"            : tp1,
            "sl"             : sl,
            "exchange"       : "Bybit",
            "timeframe"      : "-",
            "current_price"  : entry,
            "risk_reward"    : "-",
            "trend"          : "-",
            "struktur"       : "Manual order",
            "zona_kunci"     : "-",
            "sinyal"         : "Manual entry dari user",
            "invalidasi"     : "-",
            "catatan_risiko" : "-",
            "confidence"     : "Medium",
            "notes"          : f"Order manual: {text}"
        }
    except Exception as e:
        print(f"parse_manual_order error: {e}")
        return None

def fix_symbol(raw: str) -> str:
    s = raw.upper().strip()
    s = re.sub(r'(TETHER(US)?|PERPETUAL|CONTRACT|PERP|USDT|USDC|BUSD|USD|BINANCE|BYBIT|BINGX|/|-|\s.*)', '', s)
    s = s.strip()
    if not s:
        s = raw.upper().replace("/", "").replace("-", "").replace(" ", "")
        s = re.sub(r'(USDC|BUSD|USD)$', '', s)
        if not s.endswith("USDT"):
            s += "USDT"
    else:
        s += "USDT"
    return s

def hitung_qty(modal_usdt: float, leverage: int, entry_price: float, pair: str) -> float:
    posisi = modal_usdt * leverage
    qty    = posisi / entry_price
    if "BTC" in pair:
        qty = max(round(qty, 3), 0.001)
    elif "ETH" in pair:
        qty = max(round(qty, 2), 0.01)
    elif "SOL" in pair:
        qty = max(round(qty, 1), 0.1)
    else:
        qty = max(int(qty), 1)
    return qty

def validasi_levels(data: dict) -> str | None:
    try:
        direction = data.get("direction", "")
        entry = float(data.get("entry", 0))
        tp1   = float(data.get("tp1", 0))
        sl    = float(data.get("sl", 0))
        if direction == "LONG" and not (tp1 > entry > sl):
            return f"Level tidak logis untuk LONG!\nEntry:{entry} TP1:{tp1} SL:{sl}"
        if direction == "SHORT" and not (tp1 < entry < sl):
            return f"Level tidak logis untuk SHORT!\nEntry:{entry} TP1:{tp1} SL:{sl}"
    except:
        return "Gagal validasi level"
    return None

def bybit_sign(body_str: str) -> tuple:
    timestamp   = get_timestamp()
    recv_window = "10000"
    sign_str    = timestamp + BYBIT_API_KEY + recv_window + body_str
    signature   = hmac.new(BYBIT_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return timestamp, recv_window, signature

def bybit_headers(body_str: str) -> dict:
    ts, rw, sig = bybit_sign(body_str)
    return {
        "X-BAPI-API-KEY"    : BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP"  : ts,
        "X-BAPI-SIGN"       : sig,
        "X-BAPI-RECV-WINDOW": rw,
        "Content-Type"      : "application/json"
    }

async def bybit_post(endpoint: str, params: dict) -> dict:
    body_str = json.dumps(params)
    headers  = bybit_headers(body_str)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(BYBIT_BASE + endpoint, headers=headers, content=body_str)
    return r.json()

async def bybit_get(endpoint: str, params: dict) -> dict:
    query_str   = "&".join([f"{k}={v}" for k, v in params.items()])
    timestamp   = get_timestamp()
    recv_window = "10000"
    sign_str    = timestamp + BYBIT_API_KEY + recv_window + query_str
    signature   = hmac.new(BYBIT_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY"    : BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP"  : timestamp,
        "X-BAPI-SIGN"       : signature,
        "X-BAPI-RECV-WINDOW": recv_window
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(BYBIT_BASE + endpoint, headers=headers, params=params)
    return r.json()

async def set_leverage(symbol: str, leverage: int) -> dict:
    return await bybit_post("/v5/position/set-leverage", {
        "category": "linear", "symbol": symbol,
        "buyLeverage": str(leverage), "sellLeverage": str(leverage)
    })

async def place_limit_order(symbol: str, side: str, qty: float, price: float,
                            take_profit: float = None, stop_loss: float = None) -> dict:
    position_idx = 1 if side == "Buy" else 2
    params = {
        "category": "linear", "symbol": symbol,
        "side": side, "orderType": "Limit",
        "qty": str(qty), "price": str(price),
        "timeInForce": "GTC", "positionIdx": position_idx
    }
    # Pasang TP/SL langsung di order biar keapply walau posisi belum terbuka
    if take_profit is not None:
        params["takeProfit"]   = str(take_profit)
        params["tpTriggerBy"]  = "MarkPrice"
    if stop_loss is not None:
        params["stopLoss"]     = str(stop_loss)
        params["slTriggerBy"]  = "MarkPrice"
    return await bybit_post("/v5/order/create", params)

async def set_trading_stop(symbol: str, side: str, take_profit: float, stop_loss: float) -> dict:
    position_idx = 1 if side == "Buy" else 2
    return await bybit_post("/v5/position/trading-stop", {
        "category": "linear", "symbol": symbol,
        "takeProfit": str(take_profit), "stopLoss": str(stop_loss),
        "tpTriggerBy": "MarkPrice", "slTriggerBy": "MarkPrice",
        "positionIdx": position_idx
    })

async def close_partial_position(symbol: str, side: str, qty: float, price: float) -> dict:
    close_side   = "Sell" if side == "Buy" else "Buy"
    position_idx = 1 if side == "Buy" else 2
    return await bybit_post("/v5/order/create", {
        "category": "linear", "symbol": symbol,
        "side": close_side, "orderType": "Limit",
        "qty": str(qty), "price": str(price),
        "timeInForce": "GTC", "positionIdx": position_idx,
        "reduceOnly": True
    })

async def get_positions() -> dict:
    return await bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})

async def get_ticker(symbol: str) -> float | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{BYBIT_BASE}/v5/market/tickers", params={"category": "linear", "symbol": symbol})
        return float(r.json()["result"]["list"][0]["lastPrice"])
    except:
        return None

async def monitor_position(app, user_id: int, chat_id: int, monitor_data: dict):
    symbol    = monitor_data["symbol"]
    tp1       = monitor_data["tp1"]
    tp2       = monitor_data["tp2"]
    sl        = monitor_data["sl"]
    side      = monitor_data["side"]
    qty       = monitor_data["qty"]
    entry     = monitor_data["entry"]
    direction = monitor_data["direction"]
    tp1_hit   = False

    while user_id in active_monitors:
        try:
            price = await get_ticker(symbol)
            if price is None:
                await asyncio.sleep(10)
                continue

            if direction == "LONG":
                if not tp1_hit and price >= tp1:
                    tp1_hit  = True
                    half_qty = max(int(qty / 2), 1) if qty >= 2 else qty
                    await close_partial_position(symbol, side, half_qty, tp1)
                    await set_trading_stop(symbol, side, tp2, entry)
                    await app.bot.send_message(chat_id=chat_id, text=(
                        f"TP1 HIT! {symbol}\n"
                        f"Harga: {price} | TP1: {tp1}\n\n"
                        f"50% posisi ({half_qty}) close!\n"
                        f"Sisa lanjut TP2: {tp2}\n"
                        f"SL geser ke entry: {entry} (breakeven)"
                    ))
                elif tp1_hit and price >= tp2:
                    await app.bot.send_message(chat_id=chat_id, text=(
                        f"TP2 HIT! {symbol}\n"
                        f"Harga: {price} | TP2: {tp2}\n"
                        f"Full profit bro!"
                    ))
                    active_monitors.pop(user_id, None)
                    break
                elif price <= sl:
                    await app.bot.send_message(chat_id=chat_id, text=(
                        f"SL HIT! {symbol}\n"
                        f"Harga: {price} | SL: {sl}\n"
                        f"Stay disciplined bro!"
                    ))
                    active_monitors.pop(user_id, None)
                    break

            elif direction == "SHORT":
                if not tp1_hit and price <= tp1:
                    tp1_hit  = True
                    half_qty = max(int(qty / 2), 1) if qty >= 2 else qty
                    await close_partial_position(symbol, side, half_qty, tp1)
                    await set_trading_stop(symbol, side, tp2, entry)
                    await app.bot.send_message(chat_id=chat_id, text=(
                        f"TP1 HIT! {symbol}\n"
                        f"Harga: {price} | TP1: {tp1}\n\n"
                        f"50% posisi ({half_qty}) close!\n"
                        f"Sisa lanjut TP2: {tp2}\n"
                        f"SL geser ke entry: {entry} (breakeven)"
                    ))
                elif tp1_hit and price <= tp2:
                    await app.bot.send_message(chat_id=chat_id, text=(
                        f"TP2 HIT! {symbol}\n"
                        f"Harga: {price} | TP2: {tp2}\n"
                        f"Full profit bro!"
                    ))
                    active_monitors.pop(user_id, None)
                    break
                elif price >= sl:
                    await app.bot.send_message(chat_id=chat_id, text=(
                        f"SL HIT! {symbol}\n"
                        f"Harga: {price} | SL: {sl}\n"
                        f"Stay disciplined bro!"
                    ))
                    active_monitors.pop(user_id, None)
                    break

        except Exception as e:
            print(f"Monitor error: {e}")

        await asyncio.sleep(15)

async def call_groq_vision(image_b64: str) -> dict:
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            {"type": "text", "text": VISION_PROMPT}
        ]}],
        "max_tokens": 1500, "temperature": 0.1
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload)
    return r.json()

async def call_groq_chat(user_message: str, history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT_CHAT}]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_message})
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 512, "temperature": 0.7})
    return r.json()["choices"][0]["message"]["content"].strip()

def format_analysis(data: dict) -> str:
    d  = data.get("direction", "-")
    cf = data.get("confidence", "-")
    
    # Emoji direction
    if d == "LONG":
        dir_emoji = "🟢"
        dir_label = "LONG / BUY"
    else:
        dir_emoji = "🔴"
        dir_label = "SHORT / SELL"
    
    # Confidence badge
    if cf == "High":
        cf_badge = "🔥 HIGH"
    elif cf == "Medium":
        cf_badge = "⚡ MEDIUM"
    else:
        cf_badge = "🌀 LOW"
    
    # Trend emoji
    trend = data.get("trend", "-")
    trend_emoji = "📈" if "Up" in trend else "📉" if "Down" in trend else "➡️"
    
    pair = data.get("pair", "-")
    tf   = data.get("timeframe", "-")
    ex   = data.get("exchange", "-")
    
    return (
        f"╔══════════════════════╗\n"
        f"║   📊 SIGNAL TRADING  ║\n"
        f"╚══════════════════════╝\n\n"
        f"{dir_emoji} *{pair}* | {tf} | {ex}\n"
        f"Arah    : {dir_emoji} *{dir_label}*\n"
        f"Trend   : {trend_emoji} {trend}\n"
        f"Sinyal  : {cf_badge}\n"
        f"Harga   : `{data.get('current_price','-')}`\n\n"
        f"━━━━━━ 🎯 ENTRY SETUP ━━━━━━\n"
        f"📍 Entry     : `{data.get('entry','-')}`\n"
        f"📦 Zone Low  : `{data.get('entry_zone_low','-')}`\n"
        f"📦 Zone High : `{data.get('entry_zone_high','-')}`\n\n"
        f"🎯 TP1  : `{data.get('tp1','-')}`\n"
        f"🎯 TP2  : `{data.get('tp2','-')}`\n"
        f"🎯 TP3  : `{data.get('tp3','-')}`\n"
        f"🛑 SL   : `{data.get('sl','-')}`\n"
        f"⚖️  R:R  : {data.get('risk_reward','-')}\n\n"
        f"━━━━━━ 🔍 ANALISIS ━━━━━━\n"
        f"📐 Struktur  : {data.get('struktur','-')}\n"
        f"🗝️  Zona Kunci: {data.get('zona_kunci','-')}\n"
        f"📡 Sinyal    : {data.get('sinyal','-')}\n\n"
        f"━━━━━━ ⚠️ RISK MANAGEMENT ━━━━━━\n"
        f"❌ Invalidasi: {data.get('invalidasi','-')}\n"
        f"💡 Risk Note : {data.get('catatan_risiko','-')}\n\n"
        f"━━━━━━ 📝 SUMMARY ━━━━━━\n"
        f"{data.get('notes','-')}\n"
        f"\n⏰ {data.get('timeframe','-')} | 🏦 {data.get('exchange','-')}"
    )

async def cmd_posisi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Lagi cek posisi lu bro...")
    try:
        result    = await get_positions()
        positions = result.get("result", {}).get("list", [])
        open_pos  = [p for p in positions if float(p.get("size", 0)) > 0]
        if not open_pos:
            await update.message.reply_text("Ga ada open position bro saat ini.")
            return
        msg = "==== OPEN POSITIONS ====\n\n"
        for p in open_pos:
            pnl      = float(p.get("unrealisedPnl", 0))
            pnl_icon = "+" if pnl >= 0 else ""
            msg += (
                f"Pair  : {p.get('symbol','-')}\n"
                f"Side  : {p.get('side','-')}\n"
                f"Size  : {p.get('size','-')}\n"
                f"Entry : {p.get('avgPrice','-')}\n"
                f"Lev   : {p.get('leverage','-')}x\n"
                f"TP    : {p.get('takeProfit','-')}\n"
                f"SL    : {p.get('stopLoss','-')}\n"
                f"PnL   : {pnl_icon}{round(pnl,4)} USDT\n"
                f"─────────────\n"
            )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error cek posisi bro: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo       = update.message.photo[-1]
    file        = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    image_b64   = base64.b64encode(image_bytes).decode("utf-8")

    await update.message.reply_text("Lagi baca chart lu bro, sebentar...")
    result = await call_groq_vision(image_b64)

    try:
        # Ambil konten dari respons LLM. Gunakan get() agar tetap aman jika struktur berubah
        content = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        ).strip()

        # Hilangkan pembungkus markdown seperti ```json``` atau ```
        cleaned = content.replace("```json", "").replace("```", "").strip()

        # Coba decode langsung sebagai JSON
        data: dict | None = None
        if cleaned:
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                # Jika gagal, coba ambil substring yang tampak seperti objek JSON pertama
                match = re.search(r"\{.*\}", cleaned, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(0))
                    except Exception:
                        data = None

        # Jika tidak ada data valid, lempar error agar masuk ke blok except di bawah
        if not data:
            raise ValueError("Tidak dapat mem-parsing output analisis menjadi JSON")

        # Normalisasi simbol pair dan simpan ke last_analysis
        data["pair"] = fix_symbol(data.get("pair", ""))
        err     = validasi_levels(data)
        user_id = update.effective_user.id
        last_analysis[user_id] = data
        # Kirim analisis dalam format terformat
        await update.message.reply_text(format_analysis(data))
        if err:
            await update.message.reply_text(
                f"PERINGATAN:\n{err}\n\nCek manual dulu sebelum execute ya bro!"
            )
        keyboard = [
            [
                InlineKeyboardButton("✅ Mau Execute", callback_data="mau_execute"),
                InlineKeyboardButton("❌ Gausah", callback_data="cancel_order"),
            ]
        ]
        await update.message.reply_text(
            "Mau buka posisi di Bybit bro?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        if user_id not in chat_history:
            chat_history[user_id] = []
        chat_history[user_id].append(
            {
                "role": "assistant",
                "content": f"Analisis {data.get('pair')} {data.get('direction')} entry {data.get('entry')} TP1 {data.get('tp1')} TP2 {data.get('tp2')} TP3 {data.get('tp3')} SL {data.get('sl')}",
            }
        )
    except Exception as e:
        # Saat parsing gagal, kirim konten mentah agar user bisa lihat apa yang salah.
        raw_preview = json.dumps(result, indent=2)[:800] if isinstance(result, dict) else str(result)[:800]
        await update.message.reply_text(
            f"Gagal parse bro\nError: {e}\nRaw:\n{raw_preview}"
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data    = last_analysis.get(user_id)

    if query.data == "cancel_order":
        await query.edit_message_text("Oke dibatalin bro, kirim chart baru kalau mau analisis lagi.")
        return

    if query.data == "mau_execute":
        if not data:
            await query.edit_message_text("Data analisis ga ketemu bro, kirim chart dulu.")
            return
        pending_order[user_id] = {"step": "leverage", "analysis": data, "modal": 1.0}
        keyboard = [[
            InlineKeyboardButton("7x",  callback_data="lev_7"),
            InlineKeyboardButton("10x", callback_data="lev_10"),
            InlineKeyboardButton("15x", callback_data="lev_15"),
            InlineKeyboardButton("20x", callback_data="lev_20"),
        ], [
            InlineKeyboardButton("✏️ Custom", callback_data="lev_custom"),
        ]]
        await query.edit_message_text(
            f"⚡ Setup Order\n\n"
            f"Pair  : {data.get('pair')}\n"
            f"Arah  : {data.get('direction')}\n"
            f"Entry : {data.get('entry')}\n"
            f"Modal : 1 USDT (default)\n\n"
            f"Pilih leverage:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data.startswith("lev_"):
        if user_id not in pending_order:
            await query.edit_message_text("Session expired bro, kirim chart lagi.")
            return
        lev_val = query.data.replace("lev_", "")
        if lev_val == "custom":
            pending_order[user_id]["step"] = "custom_leverage"
            await query.edit_message_text("Ketik leverage yang lu mau (contoh: 25):")
            return
        leverage  = int(lev_val)
        order     = pending_order[user_id]
        modal     = order["modal"]
        analysis  = order["analysis"]
        pending_order[user_id]["leverage"] = leverage
        pending_order[user_id]["step"]     = "tp"
        tp1 = analysis.get("tp1")
        tp2 = analysis.get("tp2")
        tp3 = analysis.get("tp3")
        posisi = modal * leverage
        keyboard = [[
            InlineKeyboardButton(f"TP1 ({tp1})", callback_data="tp1"),
            InlineKeyboardButton(f"TP2 ({tp2})", callback_data="tp2"),
            InlineKeyboardButton(f"TP3 ({tp3})", callback_data="tp3"),
        ]]
        await query.edit_message_text(
            f"✅ Leverage: {leverage}x\n"
            f"💰 Modal   : {modal} USDT\n"
            f"📊 Posisi  : {posisi} USDT\n\n"
            f"Target TP mana bro?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data in ["tp1", "tp2", "tp3"]:
        if user_id not in pending_order:
            await query.edit_message_text("Session expired bro, kirim chart lagi.")
            return
        order     = pending_order[user_id]
        analysis  = order["analysis"]
        tp_map    = {"tp1": analysis.get("tp1"), "tp2": analysis.get("tp2"), "tp3": analysis.get("tp3")}
        tp_target = float(tp_map[query.data])
        await query.edit_message_text("Eksekusi order bro...")
        modal     = order["modal"]
        leverage  = order["leverage"]
        symbol    = analysis.get("pair", "")
        direction = analysis.get("direction", "LONG")
        side      = "Buy" if direction == "LONG" else "Sell"
        entry     = float(analysis.get("entry", 0))
        sl        = float(analysis.get("sl", 0))
        tp2       = float(analysis.get("tp2", tp_target))
        qty       = hitung_qty(modal, leverage, entry, symbol)
        try:
            lev = await set_leverage(symbol, leverage)
            if lev.get("retCode") not in [0, 110043]:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Warning leverage: {lev.get('retMsg')}")
            order_res = await place_limit_order(symbol, side, qty, entry,
                                                take_profit=tp_target, stop_loss=sl)
            if order_res.get("retCode") == 0:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=(
                    f"ORDER BERHASIL!\n\n"
                    f"Pair     : {symbol}\n"
                    f"Side     : {side}\n"
                    f"Leverage : {leverage}x\n"
                    f"Modal    : {modal} USDT\n"
                    f"Posisi   : {modal * leverage} USDT\n"
                    f"Qty      : {qty}\n"
                    f"Entry    : {entry}\n"
                    f"TP       : {tp_target}\n"
                    f"SL       : {sl}\n\n"
                    f"Order ID : {order_res.get('result', {}).get('orderId', '-')}\n\n"
                    f"Monitor aktif! Gw bakal notif kalau TP/SL hit bro."
                ))
                active_monitors[user_id] = {"symbol": symbol, "direction": direction, "side": side, "entry": entry, "tp1": tp_target, "tp2": tp2, "sl": sl, "qty": qty}
                asyncio.create_task(monitor_position(context.application, user_id, update.effective_chat.id, active_monitors[user_id]))
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Order gagal bro!\nError: {order_res.get('retMsg')}\nCode: {order_res.get('retCode')}")
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Error execute bro: {e}")
        pending_order.pop(user_id, None)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id      = update.effective_user.id
    user_message = update.message.text.strip()

    if user_id in pending_order:
        order = pending_order[user_id]
        if order["step"] == "modal":
            try:
                modal = float(user_message)
                if modal <= 0: raise ValueError
                pending_order[user_id]["modal"] = modal
                pending_order[user_id]["step"]  = "leverage"
                keyboard = [[
                    InlineKeyboardButton("7x",  callback_data="lev_7"),
                    InlineKeyboardButton("10x", callback_data="lev_10"),
                    InlineKeyboardButton("15x", callback_data="lev_15"),
                    InlineKeyboardButton("20x", callback_data="lev_20"),
                ], [
                    InlineKeyboardButton("✏️ Custom", callback_data="lev_custom"),
                ]]
                await update.message.reply_text(
                    f"Modal: {modal} USDT\n\nPilih leverage:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except:
                await update.message.reply_text("Angka ga valid bro, coba lagi (contoh: 50)")
            return
        if order["step"] == "custom_leverage":
            try:
                leverage = int(user_message.strip())
                if leverage < 1 or leverage > 100:
                    await update.message.reply_text("Leverage harus antara 1-100 bro!")
                    return
                modal    = order.get("modal", 1.0)
                analysis = order["analysis"]
                pending_order[user_id]["leverage"] = leverage
                pending_order[user_id]["step"]     = "tp"
                tp1 = analysis.get("tp1")
                tp2 = analysis.get("tp2")
                tp3 = analysis.get("tp3")
                posisi = modal * leverage
                keyboard = [[
                    InlineKeyboardButton(f"TP1 ({tp1})", callback_data="tp1"),
                    InlineKeyboardButton(f"TP2 ({tp2})", callback_data="tp2"),
                    InlineKeyboardButton(f"TP3 ({tp3})", callback_data="tp3"),
                ], [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]]
                await update.message.reply_text(
                    f"✅ Leverage: {leverage}x\n"
                    f"💰 Modal   : {modal} USDT\n"
                    f"📊 Posisi  : {posisi} USDT\n\n"
                    f"Target TP mana bro?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            except ValueError:
                await update.message.reply_text("Ketik angka leverage nya bro! Contoh: 25")
                return

        if order["step"] == "leverage":
            try:
                leverage = int(user_message)
                if leverage <= 0 or leverage > 100: raise ValueError
                pending_order[user_id]["leverage"] = leverage
                pending_order[user_id]["step"]     = "tp"
                analysis = order["analysis"]
                modal    = order["modal"]
                entry    = float(analysis.get("entry", 0))
                qty      = hitung_qty(modal, leverage, entry, analysis.get("pair", ""))
                keyboard = [
                    [
                        InlineKeyboardButton(f"TP1: {analysis.get('tp1')}", callback_data="tp1"),
                        InlineKeyboardButton(f"TP2: {analysis.get('tp2')}", callback_data="tp2"),
                        InlineKeyboardButton(f"TP3: {analysis.get('tp3')}", callback_data="tp3"),
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]
                ]
                await update.message.reply_text(
                    f"Konfirmasi Order:\n\n"
                    f"Pair     : {analysis.get('pair')}\n"
                    f"Arah     : {analysis.get('direction')}\n"
                    f"Leverage : {leverage}x\n"
                    f"Modal    : {modal} USDT\n"
                    f"Posisi   : {modal * leverage} USDT\n"
                    f"Qty      : {qty}\n"
                    f"Entry    : {entry}\n"
                    f"SL       : {analysis.get('sl')}\n\n"
                    f"Pilih target TP:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except:
                await update.message.reply_text("Leverage ga valid bro, coba lagi (contoh: 15)")
            return

    if user_id not in chat_history:
        chat_history[user_id] = []

    # Cek dulu apakah ini manual order format $PAIR
    if user_message.startswith("$"):
        manual = parse_manual_order(user_message)
        if manual:
            err     = validasi_levels(manual)
            last_analysis[user_id] = manual
            summary = (
                f"Manual Order Detected!\n\n"
                f"Pair      : {manual['pair']}\n"
                f"Arah      : {manual['direction']}\n"
                f"Entry     : {manual['entry']}\n"
                f"Zone Low  : {manual['entry_zone_low']}\n"
                f"Zone High : {manual['entry_zone_high']}\n"
                f"SL        : {manual['sl'] or '-'}\n"
                f"TP        : {manual['tp1'] or '-'}\n"
            )
            await update.message.reply_text(summary)
            if err:
                await update.message.reply_text(f"PERINGATAN:\n{err}")
            keyboard = [[
                InlineKeyboardButton("✅ Mau Execute", callback_data="mau_execute"),
                InlineKeyboardButton("❌ Gausah",      callback_data="cancel_order")
            ]]
            await update.message.reply_text("Mau buka posisi di Bybit bro?",
                                            reply_markup=InlineKeyboardMarkup(keyboard))
            return
        else:
            await update.message.reply_text(
                "Format kurang lengkap bro!\n\n"
                "Contoh yang bener:\n"
                "$FIDA Buy limit 0.02475 - 0.02363 SL 0.02250\n"
                "$BTC Short 95000 - 94500 SL 96000 TP 90000"
            )
            return

    try:
        reply = await call_groq_chat(user_message, chat_history[user_id])
        chat_history[user_id].append({"role": "user", "content": user_message})
        chat_history[user_id].append({"role": "assistant", "content": reply})
        if len(chat_history[user_id]) > 20:
            chat_history[user_id] = chat_history[user_id][-20:]
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(f"Error bro: {e}")


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Lagi cek saldo lu bro...")
    try:
        result = await bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        coins  = result.get("result", {}).get("list", [{}])[0].get("coin", [])
        if not coins:
            await update.message.reply_text("Saldo kosong atau akun bukan Unified bro.")
            return
        msg = "==== SALDO UNIFIED ====\n\n"
        for c in coins:
            if float(c.get("walletBalance", 0)) > 0:
                available  = float(c.get("availableToWithdraw") or c.get("availableToBorrow") or 0)
                total      = float(c.get("walletBalance", 0))
                unrealised = float(c.get("unrealisedPnl", 0))
                msg += (
                    f"Coin      : {c.get('coin')}\n"
                    f"Total     : {round(total, 4)}\n"
                    f"Available : {round(available, 4)}\n"
                    f"uPnL      : {round(unrealised, 4)}\n"
                    f"─────────────\n"
                )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error cek saldo bro: {e}")

def main():
    asyncio.get_event_loop().run_until_complete(sync_time())
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("posisi", cmd_posisi))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot trading full feature jalan bro!")
    app.run_polling()

if __name__ == "__main__":
    main()

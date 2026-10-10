"""損益計算：輸入買賣價、折扣、張數，計算手續費、證交稅、淨損益與最低不賠賣出價。"""
import math
from decimal import Decimal
import streamlit as st

from app.common.help_tooltip import apply_help_tooltip_top_right

SHARES_PER_LOT = 1000
FEE_RATE = Decimal("0.001425")
MIN_FEE_PER_SIDE = 20
TAX_RATE_NORMAL = Decimal("0.003")
TAX_RATE_DAY_TRADE = Decimal("0.0015")
TAX_RATE_ETF = Decimal("0.001")
COLOR_PROFIT = "#f8c6c6"
COLOR_LOSS = "#50C878"
MAX_BREAK_EVEN_STEPS = 200000

# (價格上限(不含), 跳動單位)，1000 以上為 5
TICK_TABLE = [
    (Decimal("10"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.1")),
    (Decimal("500"), Decimal("0.5")),
    (Decimal("1000"), Decimal("1")),
]
TICK_TOP = Decimal("5")


def tick_size(price: Decimal) -> Decimal:
    """回傳該價位適用的最小跳動單位。"""
    for upper, tick in TICK_TABLE:
        if price < upper:
            return tick
    return TICK_TOP


def is_valid_tick_price(price: Decimal) -> bool:
    """價格是否落在該價位的跳動單位格點上。"""
    return price > 0 and (price % tick_size(price)) == 0


def tax_rate(is_day_trade: bool, is_etf: bool) -> Decimal:
    """ETF 優先(0.1%)，其次當沖(0.15%)，否則一般(0.3%)。"""
    if is_etf:
        return TAX_RATE_ETF
    if is_day_trade:
        return TAX_RATE_DAY_TRADE
    return TAX_RATE_NORMAL


def calc_fee_one_side(price: Decimal, lots: int, discount: Decimal) -> int:
    """單邊手續費：無條件捨去取整數，低於 20 元以 20 元計。"""
    raw = price * SHARES_PER_LOT * lots * FEE_RATE * discount
    return max(MIN_FEE_PER_SIDE, math.floor(raw))


def calc_pnl(buy: Decimal, sell: Decimal, lots: int, discount: Decimal,
             is_day_trade: bool, is_etf: bool) -> dict:
    """計算手續費(買賣兩邊)、證交稅與淨損益，金額皆為整數元。"""
    fee = calc_fee_one_side(buy, lots, discount) + calc_fee_one_side(sell, lots, discount)
    tax = math.floor(sell * SHARES_PER_LOT * lots * tax_rate(is_day_trade, is_etf))
    gross = (sell - buy) * SHARES_PER_LOT * lots
    return {"fee": fee, "tax": tax, "net": int(gross) - fee - tax}


def find_break_even_price(buy: Decimal, lots: int, discount: Decimal,
                          is_day_trade: bool, is_etf: bool):
    """從買入價起逐檔往上找淨損益 >= 0 的最小合法賣出價，找不到回傳 None。"""
    price = buy
    for _ in range(MAX_BREAK_EVEN_STEPS):
        if calc_pnl(buy, price, lots, discount, is_day_trade, is_etf)["net"] >= 0:
            return price
        price += tick_size(price)
    return None


def prev_tick_price(price: Decimal) -> Decimal:
    """往下一檔合法價位(跨區間邊界時用下方區間的跳動單位，例如 10.00 的下一檔是 9.99)。"""
    return price - tick_size(price - Decimal("0.0001"))


def find_max_break_even_buy_price(sell: Decimal, lots: int, discount: Decimal,
                                  is_day_trade: bool, is_etf: bool):
    """做空：從賣出價起逐檔往下找淨損益 >= 0 的最高合法買入價，找不到回傳 None。"""
    price = sell
    for _ in range(MAX_BREAK_EVEN_STEPS):
        if price <= 0:
            return None
        if calc_pnl(price, sell, lots, discount, is_day_trade, is_etf)["net"] >= 0:
            return price
        price = prev_tick_price(price)
    return None


def _fmt_price(price: Decimal) -> str:
    """價格固定顯示兩位小數。"""
    return f"{price:.2f}"


def _to_decimal(value: float) -> Decimal:
    """number_input 回傳的 float 轉成兩位小數的 Decimal，避免浮點誤差。"""
    return Decimal(str(round(value, 2)))


def _next_price(cur: Decimal, direction: int) -> Decimal:
    """從 cur 往上(direction > 0)或往下走一檔合法價位；不在格點上時先吸附到最近的格點。"""
    tick = tick_size(cur) if cur > 0 else Decimal("0.01")
    on_grid = cur == 0 or cur % tick == 0
    if direction > 0:
        new = (cur // tick + 1) * tick
    elif on_grid:
        new = prev_tick_price(cur) if cur > 0 else Decimal("0")
    else:
        new = (cur // tick) * tick
    return max(new, Decimal("0"))


def _on_price_change(state_key: str, widget_key: str, step: Decimal):
    """number_input 的 on_change：偵測是否為內建 +/- 的一次點擊，是的話改走正確的一檔。

    內建 +/- 只有單一 step(這裡設成目前價位的跳動單位)，所以往上沒問題，
    但在區間邊界往下會跳過頭(例如 100 - 0.5 = 99.5，應為 99.9)。
    這裡比對「新值 - 舊值」是否剛好等於 step，是的話就用 _next_price() 重算。
    使用者直接打字輸入的值則原樣採用。
    """
    prev = _to_decimal(st.session_state.get(state_key, 0.0))
    new = _to_decimal(st.session_state[widget_key])
    if abs(new - prev) == step:
        new = _next_price(prev, 1 if new > prev else -1)
        st.session_state[widget_key] = float(new)
    st.session_state[state_key] = float(new)


def _price_input(container, label: str, state_key: str) -> float:
    """價格輸入框：內建 +/- 的步進依目前價位所屬區間的跳動單位動態調整。

    step 改變會讓 Streamlit 把元件視為新元件，所以把 step 放進元件 key，
    並用 state_key 另存目前價格，重建時把它寫回元件狀態。
    """
    stored = float(st.session_state.get(state_key, 0.0))
    step = tick_size(_to_decimal(stored)) if stored > 0 else Decimal("0.01")
    widget_key = f"{state_key}_widget_{step}"
    # 每次都以 stored 為準寫回元件狀態：換 step 後的新元件、以及先前殘留的舊 key 都會因此取得正確的值
    st.session_state[widget_key] = stored
    value = container.number_input(
        label, min_value=0.0, step=float(step), format="%.2f", key=widget_key,
        on_change=_on_price_change, args=(state_key, widget_key, step),
    )
    st.session_state[state_key] = value
    return value


def render_pnl_calculator_tab():
    """損益計算分頁。"""
    apply_help_tooltip_top_right()
    st.header(
        "損益計算",
        help=(
            "做多、做空使用相同公式：淨損益 = (賣出價 - 買入價) × 1000 × 張數 - 手續費 - 證交稅。"
            "手續費買賣兩邊各自無條件捨去取整數，每邊最低 20 元；證交稅同樣無條件捨去取整數。"
            "證交稅率：一般 0.3%、當沖 0.15%、ETF 0.1%(同時勾選時以 ETF 為準)。"
        ),
    )

    direction = st.radio("交易方向", ["做多", "做空"], index=0, horizontal=True, key="pnl_direction",
                         help="做空：先賣後買。輸入賣出價後，顯示最高不賠買入價。")
    is_short = direction == "做空"

    col1, col2 = st.columns(2)
    # 做空時賣出價在前(先賣出)，做多時買入價在前(先買進)；輸入值各自存在固定的 state key
    buy_col, sell_col = (col2, col1) if is_short else (col1, col2)
    buy_raw = _price_input(buy_col, "買入價(回補價)" if is_short else "買入價", "pnl_buy")
    sell_raw = _price_input(sell_col, "賣出價(放空價)" if is_short else "賣出價", "pnl_sell")
    with col1:
        discount_raw = st.number_input(
            "手續費折扣比例 (%)", min_value=0.0, max_value=100.0, value=62.0, step=0.1,
            format="%.1f", key="pnl_discount",
            help="62 代表 62 折，實際手續費率 = 0.1425% × 62%。",
        )
    with col2:
        lots = st.number_input("張數", min_value=1, value=1, step=1, key="pnl_lots")

    chk1, chk2 = st.columns(2)
    is_day_trade = chk1.checkbox("當沖", value=False, key="pnl_day_trade")
    is_etf = chk2.checkbox("ETF", value=False, key="pnl_etf",
                           help="同時勾選當沖與 ETF 時，證交稅以 ETF 稅率計算。")

    buy = _to_decimal(buy_raw)
    sell = _to_decimal(sell_raw)
    discount = _to_decimal(discount_raw) / 100 if discount_raw else Decimal("0")
    lots = int(lots)

    buy_ok = buy > 0 and is_valid_tick_price(buy)
    sell_ok = sell > 0 and is_valid_tick_price(sell)

    st.subheader("損益結果")
    if buy > 0 and not buy_ok:
        st.error(f"買入價 {_fmt_price(buy)} 不是合法價位(此價位的跳動單位為 {tick_size(buy)})，無法計算。")
    if sell > 0 and not sell_ok:
        st.error(f"賣出價 {_fmt_price(sell)} 不是合法價位(此價位的跳動單位為 {tick_size(sell)})，無法計算。")

    if buy_ok and sell_ok:
        result = calc_pnl(buy, sell, lots, discount, is_day_trade, is_etf)
        m1, m2 = st.columns(2)
        m1.metric("手續費(買賣合計)", f"{result['fee']:,} 元")
        m2.metric("證交稅", f"{result['tax']:,} 元")
        bg = COLOR_PROFIT if result["net"] > 0 else COLOR_LOSS if result["net"] < 0 else "transparent"
        st.markdown(
            f"<div style='background:{bg};padding:12px 16px;border-radius:8px;"
            f"font-size:1.4rem;font-weight:600;color:#222'>淨損益：{result['net']:,} 元</div>",
            unsafe_allow_html=True,
        )
    elif buy == 0 or sell == 0:
        st.info("輸入買入價與賣出價。")

    if is_short:
        st.subheader("最高不賠買入價")
        if not sell_ok:
            st.info("輸入合法的賣出價，會自動計算最高不賠買入價。")
            return
        break_even = find_max_break_even_buy_price(sell, lots, discount, is_day_trade, is_etf)
        label = "最高可用買入價(回補價)"
    else:
        st.subheader("最低不賠賣出價")
        if not buy_ok:
            st.info("輸入合法的買入價，系統會自動計算最低不賠賣出價。")
            return
        break_even = find_break_even_price(buy, lots, discount, is_day_trade, is_etf)
        label = "至少要賣出"
    if break_even is None:
        st.warning("在合理範圍內找不到不賠的價位，檢查輸入條件。")
        return
    st.metric(label, f"{_fmt_price(break_even)} 元")
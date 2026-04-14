import json
import math
import signal
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import pandas as pd
from ib_insync import IB, Stock, Forex, MarketOrder, util


@dataclass
class BotConfig:
    host: str = "127.0.0.1"
    port: int = 7497  # paper TWS commonly uses 7497
    client_id: int = 101

    symbol: str = "AAPL"
    sec_type: str = "STK"  # STK or CASH
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: Optional[str] = None

    bar_size: str = "1 min"
    duration: str = "3 D"
    use_rth: bool = True

    sma_window: int = 20
    num_std: float = 2.0

    base_order_qty: int = 0  # start at 0 for connection-only test, then change to 1
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.015
    max_position_abs: int = 1
    cooldown_seconds: int = 5

    timezone: str = "America/New_York"
    session_start: str = "09:35"
    session_end: str = "15:55"
    allow_short: bool = True

    state_dir: str = "bot_state"
    state_file: str = "state.json"
    trade_log_file: str = "trades.csv"
    exec_log_file: str = "executions.csv"
    heartbeat_seconds: int = 30

    flatten_on_shutdown: bool = False
    debug: bool = True


@dataclass
class BotState:
    current_position: int = 0
    entry_price: Optional[float] = None
    entry_time: Optional[str] = None
    last_processed_bar_time: Optional[str] = None
    last_order_time: Optional[str] = None
    pending_order: bool = False
    pending_order_id: Optional[int] = None
    cumulative_realized_pnl: float = 0.0
    last_signal: int = 0
    last_error: Optional[str] = None
    partial_fill_qty: int = 0


class IBBollingerBot:
    def __init__(self, config: BotConfig):
        self.cfg = config
        self.ib = IB()
        self.contract = None

        self.state_path = Path(self.cfg.state_dir) / self.cfg.state_file
        self.trade_log_path = Path(self.cfg.state_dir) / self.cfg.trade_log_file
        self.exec_log_path = Path(self.cfg.state_dir) / self.cfg.exec_log_file

        Path(self.cfg.state_dir).mkdir(parents=True, exist_ok=True)

        self.state = self._load_state()
        self.bars = None
        self.last_heartbeat = 0.0
        self.account_values = {}
        self.is_shutting_down = False

    def log(self, msg: str):
        now = datetime.utcnow().isoformat()
        print(f"[{now}] {msg}")

    def _load_state(self) -> BotState:
        if self.state_path.exists():
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return BotState(**data)
        return BotState()

    def _save_state(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self.state), f, indent=2)

    def _append_csv_row(self, path: Path, row: dict):
        df = pd.DataFrame([row])
        header = not path.exists()
        df.to_csv(path, mode="a", header=header, index=False)

    def build_contract(self):
        if self.cfg.sec_type == "STK":
            c = Stock(self.cfg.symbol, self.cfg.exchange, self.cfg.currency)
            if self.cfg.primary_exchange:
                c.primaryExchange = self.cfg.primary_exchange
            return c
        if self.cfg.sec_type == "CASH":
            return Forex(f"{self.cfg.symbol}{self.cfg.currency}")
        raise ValueError(f"Unsupported sec_type: {self.cfg.sec_type}")

    def connect(self):
        self.log("Connecting to IBKR...")
        self.ib.connect(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id)
        self.contract = self.build_contract()
        self.ib.qualifyContracts(self.contract)

        self.ib.connectedEvent += self._on_connected
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.execDetailsEvent += self._on_exec_details
        self.ib.orderStatusEvent += self._on_order_status

        self.sync_account()
        self.sync_positions()
        self.start_bar_stream()

    def _on_connected(self):
        self.log("Connected event fired.")
        self.sync_account()
        self.sync_positions()
        if self.bars is None:
            self.start_bar_stream()

    def _on_disconnected(self):
        self.log("Disconnected from IBKR.")

    def ensure_connection(self):
        if self.ib.isConnected():
            return
        self.log("Connection lost. Attempting reconnect...")
        while not self.ib.isConnected() and not self.is_shutting_down:
            try:
                self.ib.connect(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id)
                self.sync_account()
                self.sync_positions()
                self.start_bar_stream()
                self.log("Reconnect successful.")
                return
            except Exception as e:
                self.state.last_error = f"Reconnect failed: {e}"
                self._save_state()
                self.log(self.state.last_error)
                time.sleep(5)

    def sync_account(self):
        try:
            vals = self.ib.accountSummary()
            self.account_values = {v.tag: v.value for v in vals}
            if self.cfg.debug:
                nlv = self.account_values.get("NetLiquidation")
                cash = self.account_values.get("TotalCashValue")
                self.log(f"Account synced. NetLiq={nlv}, Cash={cash}")
        except Exception as e:
            self.log(f"Account sync failed: {e}")

    def sync_positions(self):
        try:
            positions = self.ib.positions()
            matched = [p for p in positions if p.contract.conId == self.contract.conId]
            qty = int(sum(p.position for p in matched)) if matched else 0

            self.state.current_position = qty
            if qty == 0:
                self.state.entry_price = None
                self.state.entry_time = None
                self.state.partial_fill_qty = 0

            self.state.pending_order = False
            self.state.pending_order_id = None
            self._save_state()
            self.log(f"Position synced from IBKR. Position={qty}")
        except Exception as e:
            self.log(f"Position sync failed: {e}")

    def start_bar_stream(self):
        if self.bars is not None:
            try:
                self.ib.cancelHistoricalData(self.bars)
            except Exception:
                pass

        self.log("Starting live historical bar stream...")
        self.bars = self.ib.reqHistoricalData(
            self.contract,
            endDateTime="",
            durationStr=self.cfg.duration,
            barSizeSetting=self.cfg.bar_size,
            whatToShow="TRADES" if self.cfg.sec_type == "STK" else "MIDPOINT",
            useRTH=self.cfg.use_rth,
            formatDate=1,
            keepUpToDate=True,
        )
        self.bars.updateEvent += self.on_bar_update

    def bars_to_df(self) -> pd.DataFrame:
        df = util.df(self.bars)
        if df.empty:
            return df
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df

    def _session_bounds(self):
        start_h, start_m = map(int, self.cfg.session_start.split(":"))
        end_h, end_m = map(int, self.cfg.session_end.split(":"))
        return dtime(start_h, start_m), dtime(end_h, end_m)

    def in_trading_session(self, ts: pd.Timestamp) -> bool:
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        local_ts = ts.tz_convert(self.cfg.timezone)
        if local_ts.weekday() >= 5:
            return False
        start_t, end_t = self._session_bounds()
        return start_t <= local_ts.time() <= end_t

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma"] = out["close"].rolling(self.cfg.sma_window).mean()
        out["std"] = out["close"].rolling(self.cfg.sma_window).std(ddof=0)
        out["upper"] = out["sma"] + self.cfg.num_std * out["std"]
        out["lower"] = out["sma"] - self.cfg.num_std * out["std"]
        return out

    def compute_signal(self, row: pd.Series) -> int:
        price = row["close"]
        sma = row["sma"]
        upper = row["upper"]
        lower = row["lower"]

        if any(pd.isna(x) for x in [price, sma, upper, lower]):
            return 0
        if price < lower:
            return 1
        if price > upper and self.cfg.allow_short:
            return -1
        return 0

    def stop_or_take_profit_hit(self, last_price: float) -> Optional[str]:
        pos = self.state.current_position
        entry = self.state.entry_price

        if pos == 0 or entry is None:
            return None

        if pos > 0:
            if last_price <= entry * (1 - self.cfg.stop_loss_pct):
                return "stop_loss"
            if last_price >= entry * (1 + self.cfg.take_profit_pct):
                return "take_profit"
        elif pos < 0:
            if last_price >= entry * (1 + self.cfg.stop_loss_pct):
                return "stop_loss"
            if last_price <= entry * (1 - self.cfg.take_profit_pct):
                return "take_profit"
        return None

    def _cooldown_active(self) -> bool:
        if not self.state.last_order_time:
            return False
        last = pd.Timestamp(self.state.last_order_time)
        return (pd.Timestamp.utcnow() - last).total_seconds() < self.cfg.cooldown_seconds

    def _mark_order_pending(self, trade):
        self.state.pending_order = True
        self.state.pending_order_id = trade.order.orderId
        self.state.last_order_time = pd.Timestamp.utcnow().isoformat()
        self._save_state()

    def _clear_pending(self):
        self.state.pending_order = False
        self.state.pending_order_id = None
        self._save_state()

    def place_market_order(self, action: str, quantity: int, reason: str):
        if quantity <= 0:
            return None
        if self.state.pending_order:
            self.log("Skipped order: pending order already exists.")
            return None
        if self._cooldown_active():
            self.log("Skipped order: cooldown active.")
            return None

        order = MarketOrder(action, quantity)
        trade = self.ib.placeOrder(self.contract, order)
        self._mark_order_pending(trade)
        self.log(f"Placed {action} {quantity} for {self.cfg.symbol}. Reason={reason}")
        return trade

    def flatten_position(self, reason: str):
        pos = self.state.current_position
        if pos == 0:
            return None
        action = "SELL" if pos > 0 else "BUY"
        qty = abs(pos)
        return self.place_market_order(action, qty, reason=reason)

    def rebalance_to_target(self, target_position: int, reason: str):
        current = self.state.current_position
        delta = target_position - current
        if delta == 0:
            return None

        if abs(target_position) > self.cfg.max_position_abs:
            self.log(f"Target {target_position} exceeds max position. Clipping.")
            target_position = int(math.copysign(self.cfg.max_position_abs, target_position))
            delta = target_position - current

        action = "BUY" if delta > 0 else "SELL"
        qty = abs(delta)
        return self.place_market_order(action, qty, reason=reason)

    def _on_exec_details(self, trade, fill):
        try:
            ts = pd.Timestamp.utcnow().isoformat()
            exec_row = {
                "timestamp": ts,
                "symbol": self.cfg.symbol,
                "side": fill.execution.side,
                "shares": fill.execution.shares,
                "price": fill.execution.price,
                "permId": fill.execution.permId,
                "orderId": fill.execution.orderId,
                "execId": fill.execution.execId,
            }
            self._append_csv_row(self.exec_log_path, exec_row)
            self.state.partial_fill_qty += int(fill.execution.shares)
            self._save_state()
            self.log(
                f"Execution received. Side={fill.execution.side}, Qty={fill.execution.shares}, Price={fill.execution.price}"
            )
        except Exception as e:
            self.log(f"Exec details handler failed: {e}")

    def _on_order_status(self, trade):
        try:
            status = trade.orderStatus.status
            filled = trade.orderStatus.filled
            remaining = trade.orderStatus.remaining
            avg_fill = trade.orderStatus.avgFillPrice

            self.log(
                f"Order status. Id={trade.order.orderId}, Status={status}, Filled={filled}, Remaining={remaining}, AvgFill={avg_fill}"
            )

            if status in {"Filled", "Cancelled", "Inactive"}:
                self._clear_pending()
                self.sync_positions()

                pos = self.state.current_position
                if pos != 0 and avg_fill and avg_fill > 0:
                    self.state.entry_price = float(avg_fill)
                    self.state.entry_time = pd.Timestamp.utcnow().isoformat()
                elif pos == 0:
                    self.state.entry_price = None
                    self.state.entry_time = None

                self._save_state()
        except Exception as e:
            self.log(f"Order status handler failed: {e}")

    def on_bar_update(self, bars, has_new_bar: bool):
        try:
            self.ensure_connection()
            if not has_new_bar:
                return

            df = self.bars_to_df()
            if len(df) < self.cfg.sma_window + 2:
                return

            last_bar = df.iloc[-1]
            bar_time = pd.Timestamp(last_bar["date"])

            if self.state.last_processed_bar_time == bar_time.isoformat():
                return

            if not self.in_trading_session(bar_time):
                if self.cfg.debug:
                    self.log(f"Outside session. Ignoring bar at {bar_time}.")
                self.state.last_processed_bar_time = bar_time.isoformat()
                self._save_state()
                return

            df = self.compute_indicators(df)
            last = df.iloc[-1]

            price = float(last["close"])
            signal_value = self.compute_signal(last)

            self.log(
                f"Bar {bar_time} | close={price:.4f} sma={last['sma']:.4f} upper={last['upper']:.4f} lower={last['lower']:.4f} signal={signal_value} pos={self.state.current_position}"
            )

            risk_reason = self.stop_or_take_profit_hit(price)
            if risk_reason:
                self.flatten_position(reason=risk_reason)
                self.state.last_processed_bar_time = bar_time.isoformat()
                self.state.last_signal = signal_value
                self._save_state()
                return

            if signal_value == 1:
                target = self.cfg.base_order_qty
            elif signal_value == -1:
                target = -self.cfg.base_order_qty
            else:
                target = 0

            if not self.state.pending_order:
                self.rebalance_to_target(target_position=target, reason="bollinger_signal")
            else:
                self.log("Pending order exists; not sending new order.")

            self.state.last_processed_bar_time = bar_time.isoformat()
            self.state.last_signal = signal_value
            self._save_state()

        except Exception as e:
            self.state.last_error = str(e)
            self._save_state()
            self.log(f"Bar handler error: {e}")

    def heartbeat(self):
        now = time.time()
        if now - self.last_heartbeat < self.cfg.heartbeat_seconds:
            return
        self.last_heartbeat = now
        self.log(
            f"Heartbeat | connected={self.ib.isConnected()} pos={self.state.current_position} entry={self.state.entry_price} pending={self.state.pending_order}"
        )

    def shutdown(self, signum=None, frame=None):
        self.log("Shutdown requested.")
        self.is_shutting_down = True

        try:
            if self.cfg.flatten_on_shutdown:
                self.sync_positions()
                self.flatten_position(reason="shutdown")
                self.ib.sleep(2)
        except Exception as e:
            self.log(f"Error flattening on shutdown: {e}")

        try:
            self._save_state()
            if self.bars is not None:
                self.ib.cancelHistoricalData(self.bars)
        except Exception:
            pass

        try:
            if self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            pass

        self.log("Shutdown complete.")
        sys.exit(0)

    def run(self):
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        self.connect()
        self.log("Bot running.")
        while True:
            self.ensure_connection()
            self.heartbeat()
            self.ib.sleep(1)


if __name__ == "__main__":
    cfg = BotConfig(
        symbol="AAPL",
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        primary_exchange="NASDAQ",
        bar_size="1 min",
        duration="3 D",
        use_rth=True,
        sma_window=20,
        num_std=2.0,
        base_order_qty=1,  # keep at 0 for first test; change to 1 after connection/bar test works
        stop_loss_pct=0.01,
        take_profit_pct=0.015,
        max_position_abs=1,
        cooldown_seconds=5,
        timezone="America/New_York",
        session_start="09:35",
        session_end="15:55",
        allow_short=True,
        state_dir="bot_state",
        flatten_on_shutdown=False,
        debug=True,
    )

    bot = IBBollingerBot(cfg)
    bot.run()


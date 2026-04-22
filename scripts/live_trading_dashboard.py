#!/usr/bin/env python3
"""Live trading dashboard for Binance or replayed market data.

This is a lightweight visualization layer on top of the existing simulation
workflow. It does not replace the RTL testbenches; instead, it shows a live
market feed, synthetic order generation, execution events, and simulated
end-to-end latency in a rolling chart.
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import random
import re
import subprocess
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, Optional

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from websocket import WebSocketApp


@dataclass
class MarketTick:
    source_ts: float
    recv_ts: float
    ts: float
    symbol: str
    mid: float
    bid: float
    ask: float
    source: str

    @property
    def source_lag_us(self) -> float:
        return max((self.recv_ts - self.source_ts) * 1_000_000.0, 0.0)

    @property
    def dashboard_lag_us(self) -> float:
        return max((self.ts - self.recv_ts) * 1_000_000.0, 0.0)


@dataclass
class OrderEvent:
    order_id: int
    side: str
    source_ts: float
    recv_ts: float
    signal_ts: float
    exec_ts: float
    price: float

    @property
    def queue_lag_us(self) -> float:
        return max((self.signal_ts - self.recv_ts) * 1_000_000.0, 0.0)


@dataclass
class ExecEvent:
    order_id: int
    side: str
    source_ts: float
    recv_ts: float
    signal_ts: float
    exec_ts: float
    price: float
    latency_us: float

    @property
    def end_to_end_us(self) -> float:
        return max((self.exec_ts - self.source_ts) * 1_000_000.0, 0.0)


@dataclass
class QuoteEvent:
    quote_id: int
    source_ts: float
    recv_ts: float
    sim_start_ts: float
    sim_end_ts: float
    mid: float
    bid: float
    ask: float
    latency_cycles: int
    latency_ns: float
    volatility: float

    @property
    def compute_lag_us(self) -> float:
        return max((self.sim_end_ts - self.sim_start_ts) * 1_000_000.0, 0.0)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_hjb_quote_output(output_path: Path) -> tuple[float, float]:
    text = output_path.read_text(encoding="utf-8").strip()
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        raise ValueError(f"Unexpected HJB quote output: {text!r}")
    return float(parts[0]), float(parts[1])


def _parse_latency_cycles(stdout: str) -> int:
    match = re.search(r"Latency:\s*(\d+)\s*cycles", stdout)
    if not match:
        raise ValueError("Unable to parse HJB latency from simulator output")
    return int(match.group(1))


def _annualized_volatility(mids: Deque[float]) -> float:
    if len(mids) < 3:
        return 0.3
    values = list(mids)
    returns = []
    for prev, curr in zip(values, values[1:]):
        if prev > 0 and curr > 0:
            returns.append(math.log(curr / prev))
    if len(returns) < 2:
        return 0.3
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return min(max(math.sqrt(max(variance, 0.0)) * math.sqrt(31_536_000), 0.01), 2.0)


class StrategySim:
    def __init__(self, latency_us: int, spread_threshold_bps: float, window: int) -> None:
        self.latency_us = latency_us
        self.spread_threshold_bps = spread_threshold_bps
        self.window = window
        self.mid_history: Deque[float] = deque(maxlen=window)
        self.pending_orders: Deque[OrderEvent] = deque()
        self.next_order_id = 1

    def observe(self, tick: MarketTick) -> Optional[OrderEvent]:
        self.mid_history.append(tick.mid)
        if len(self.mid_history) < 8:
            return None

        avg_mid = sum(self.mid_history) / len(self.mid_history)
        deviation_bps = ((tick.mid - avg_mid) / avg_mid) * 10_000.0
        spread_bps = ((tick.ask - tick.bid) / tick.mid) * 10_000.0 if tick.mid > 0 else 0.0

        # Simple ultra-low-latency style signal: lean against fast dislocations
        if abs(deviation_bps) >= self.spread_threshold_bps or spread_bps >= self.spread_threshold_bps:
            side = "BUY" if deviation_bps < 0 else "SELL"
            order = OrderEvent(
                order_id=self.next_order_id,
                side=side,
                source_ts=tick.source_ts,
                recv_ts=tick.recv_ts,
                signal_ts=tick.ts,
                exec_ts=tick.ts + (self.latency_us / 1_000_000.0),
                price=tick.mid,
            )
            self.next_order_id += 1
            self.pending_orders.append(order)
            return order

        return None

    def due_executions(self, now_ts: float) -> Iterable[ExecEvent]:
        while self.pending_orders and self.pending_orders[0].exec_ts <= now_ts:
            order = self.pending_orders.popleft()
            yield ExecEvent(
                order_id=order.order_id,
                side=order.side,
                source_ts=order.source_ts,
                recv_ts=order.recv_ts,
                signal_ts=order.signal_ts,
                exec_ts=order.exec_ts,
                price=order.price,
                latency_us=(order.exec_ts - order.signal_ts) * 1_000_000.0,
            )


class HJBEngine:
    def __init__(
        self,
        pump: "EventPump",
        latency_guard: float = 60.0,
        fill_model: str = "simulated",
        fill_probability: float = 0.35,
    ) -> None:
        self.pump = pump
        self.repo_root = _repo_root()
        self.market_input = self.repo_root / "market_input.txt"
        self.strategy_output = self.repo_root / "strategy_output.txt"
        self.hjb_exe = self.repo_root / "sim" / "hjb_calculator_tb"
        self.queue: "queue.Queue[Optional[MarketTick]]" = queue.Queue(maxsize=10_000)
        self.mid_history: Deque[float] = deque(maxlen=240)
        self.latency_guard = latency_guard
        self.fill_model = fill_model
        self.fill_probability = max(0.0, min(fill_probability, 1.0))
        self.quote_id = 1
        self.fill_side_toggle = 0
        self.dropped_ticks = 0
        self.max_queue_depth = 0
        self._ensure_built()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _ensure_built(self) -> None:
        if self.hjb_exe.exists():
            return
        subprocess.run(["make", "iverilog-hjb"], cwd=self.repo_root, check=True)

    def submit_tick(self, tick: MarketTick) -> None:
        try:
            self.queue.put_nowait(tick)
            self.max_queue_depth = max(self.max_queue_depth, self.queue.qsize())
        except queue.Full:
            self.dropped_ticks += 1
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(tick)
                self.max_queue_depth = max(self.max_queue_depth, self.queue.qsize())
            except queue.Full:
                self.dropped_ticks += 1
                pass

    def metrics(self) -> Dict[str, int]:
        return {
            "depth": self.queue.qsize(),
            "dropped": self.dropped_ticks,
            "max_depth": self.max_queue_depth,
        }

    def stop(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            tick = self.queue.get()
            if tick is None:
                return

            self.mid_history.append(tick.mid)
            volatility = _annualized_volatility(self.mid_history)
            sim_start_ts = time.time()

            self.market_input.write_text(f"{tick.mid:.8f},0,{volatility:.6f}\n", encoding="utf-8")
            result = subprocess.run(
                [str(self.hjb_exe)],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.latency_guard,
                check=False,
            )
            sim_end_ts = time.time()

            latency_cycles = _parse_latency_cycles(result.stdout)
            latency_ns = latency_cycles * 4.0
            bid, ask = _parse_hjb_quote_output(self.strategy_output)

            quote_event = {
                "kind": "quote",
                "quote_id": self.quote_id,
                "source_ts": tick.source_ts,
                "recv_ts": tick.recv_ts,
                "sim_start_ts": sim_start_ts,
                "sim_end_ts": sim_end_ts,
                "ts": sim_end_ts,
                "symbol": tick.symbol,
                "mid": tick.mid,
                "bid": bid,
                "ask": ask,
                "latency_cycles": latency_cycles,
                "latency_ns": latency_ns,
                "volatility": volatility,
                "source": "hjb-rtl",
            }
            self.pump.put(quote_event)

            fill_side: Optional[str] = None
            fill_price = 0.0
            if self.fill_model == "strict":
                if tick.ask <= bid:
                    fill_side = "BUY"
                    fill_price = bid
                elif tick.bid >= ask:
                    fill_side = "SELL"
                    fill_price = ask
            else:
                # Simulated maker fill model: emit fills probabilistically so the
                # dashboard can visualize executions even when strict crossing is rare.
                if random.random() <= self.fill_probability:
                    fill_side = "BUY" if self.fill_side_toggle % 2 == 0 else "SELL"
                    fill_price = tick.ask if fill_side == "BUY" else tick.bid
                    self.fill_side_toggle += 1

            if fill_side is not None:
                exec_event = {
                    "kind": "execution",
                    "order_id": self.quote_id,
                    "side": fill_side,
                    "source_ts": tick.source_ts,
                    "recv_ts": tick.recv_ts,
                    "signal_ts": sim_start_ts,
                    "exec_ts": sim_end_ts,
                    "ts": sim_end_ts,
                    "symbol": tick.symbol,
                    "price": fill_price,
                    "latency_us": latency_ns / 1000.0,
                    "execution_model": self.fill_model,
                    "source": "hjb-fill",
                }
                self.pump.put(exec_event)

            self.quote_id += 1


class Dashboard:
    def __init__(
        self,
        title: str,
        max_points: int,
        max_orders: int,
        backend: str,
        quote_max_deviation_pct: float,
    ) -> None:
        self.title = title
        self.max_points = max_points
        self.max_orders = max_orders
        self.backend = backend
        self.quote_max_deviation_pct = max(0.01, quote_max_deviation_pct)

        self.times: Deque[float] = deque(maxlen=max_points)
        self.mids: Deque[float] = deque(maxlen=max_points)
        self.bids: Deque[float] = deque(maxlen=max_points)
        self.asks: Deque[float] = deque(maxlen=max_points)

        self.order_times: Deque[float] = deque(maxlen=max_orders)
        self.order_prices: Deque[float] = deque(maxlen=max_orders)
        self.order_sides: Deque[str] = deque(maxlen=max_orders)
        self.exec_times: Deque[float] = deque(maxlen=max_orders)
        self.exec_prices: Deque[float] = deque(maxlen=max_orders)
        self.exec_sides: Deque[str] = deque(maxlen=max_orders)
        self.latencies_us: Deque[float] = deque(maxlen=max_orders)
        self.source_lags_us: Deque[float] = deque(maxlen=max_points)
        self.dashboard_lags_us: Deque[float] = deque(maxlen=max_points)
        self.queue_lags_us: Deque[float] = deque(maxlen=max_orders)
        self.end_to_end_us: Deque[float] = deque(maxlen=max_orders)
        self.quote_times: Deque[float] = deque(maxlen=max_points)
        self.quote_bids: Deque[float] = deque(maxlen=max_points)
        self.quote_asks: Deque[float] = deque(maxlen=max_points)
        self.compute_lags_us: Deque[float] = deque(maxlen=max_points)
        self.latency_cycles: Deque[int] = deque(maxlen=max_points)

        self.total_ticks = 0
        self.total_orders = 0
        self.total_execs = 0
        self.last_symbol = "-"
        self.last_source = "-"
        self.last_latency = 0.0
        self.last_event_wall = time.time()
        self.pump_depth = 0
        self.pump_dropped = 0
        self.pump_max_depth = 0
        self.hjb_depth = 0
        self.hjb_dropped = 0
        self.hjb_max_depth = 0

        self.fig, (self.ax_price, self.ax_latency) = plt.subplots(
            2,
            1,
            figsize=(13, 8),
            gridspec_kw={"height_ratios": [3, 1]},
        )
        self.fig.patch.set_facecolor("#0b1220")
        self.ax_price.set_facecolor("#111827")
        self.ax_latency.set_facecolor("#111827")
        self.fig.canvas.manager.set_window_title(self.title)
        self.fig.suptitle(self.title, fontsize=17, fontweight="bold", color="#f8fafc")

        self.price_line, = self.ax_price.plot([], [], label="Mid", color="#00bcd4", linewidth=1.8)
        self.bid_line, = self.ax_price.plot([], [], label="Bid", color="#4caf50", linewidth=1.0, alpha=0.8)
        self.ask_line, = self.ax_price.plot([], [], label="Ask", color="#ff9800", linewidth=1.0, alpha=0.8)
        self.quote_bid_line, = self.ax_price.plot([], [], label="HJB Bid", color="#8e44ad", linewidth=1.4, linestyle="--")
        self.quote_ask_line, = self.ax_price.plot([], [], label="HJB Ask", color="#c0392b", linewidth=1.4, linestyle="--")
        self.order_scatter = self.ax_price.scatter([], [], s=35, marker="^", color="#ffeb3b", label="Orders")
        self.exec_scatter = self.ax_price.scatter([], [], s=60, marker="o", color="#ff5252", label="Executions")
        self.status_text = self.ax_price.text(
            0.01,
            0.97,
            "",
            transform=self.ax_price.transAxes,
            va="top",
            ha="left",
            fontsize=10,
            family="monospace",
            bbox={"facecolor": "#000000", "alpha": 0.60, "pad": 7, "edgecolor": "#374151"},
            color="#f9fafb",
        )

        self.ax_price.set_ylabel("Price", color="#e5e7eb")
        self.ax_price.grid(True, alpha=0.25)
        self.ax_price.legend(loc="upper right")

        self.source_lag_line, = self.ax_latency.plot([], [], label="Source lag", color="#3498db", linewidth=1.4)
        self.dashboard_lag_line, = self.ax_latency.plot([], [], label="Dashboard lag", color="#9b59b6", linewidth=1.2)
        self.exec_latency_line, = self.ax_latency.plot([], [], label="Exec latency", color="#e67e22", linewidth=1.2)
        self.ax_latency.set_ylabel("Lag / latency (us)", color="#e5e7eb")
        self.ax_latency.set_xlabel("Recent samples", color="#e5e7eb")
        self.ax_latency.grid(True, axis="y", alpha=0.25)
        self.ax_latency.legend(loc="upper right")
        self.ax_price.tick_params(colors="#d1d5db")
        self.ax_latency.tick_params(colors="#d1d5db")

    def push_tick(self, tick: MarketTick) -> None:
        self.times.append(tick.ts)
        self.mids.append(tick.mid)
        self.bids.append(tick.bid)
        self.asks.append(tick.ask)
        self.source_lags_us.append(tick.source_lag_us)
        self.dashboard_lags_us.append(tick.dashboard_lag_us)
        self.total_ticks += 1
        self.last_symbol = tick.symbol
        self.last_source = tick.source
        self.last_event_wall = time.time()

    def push_order(self, order: OrderEvent) -> None:
        self.order_times.append(order.signal_ts)
        self.order_prices.append(order.price)
        self.order_sides.append(order.side)
        self.queue_lags_us.append(order.queue_lag_us)
        self.total_orders += 1

    def push_execution(self, exec_event: ExecEvent) -> None:
        self.exec_times.append(exec_event.exec_ts)
        self.exec_prices.append(exec_event.price)
        self.exec_sides.append(exec_event.side)
        self.latencies_us.append(exec_event.latency_us)
        self.end_to_end_us.append(exec_event.end_to_end_us)
        self.total_execs += 1
        self.last_latency = exec_event.latency_us
        self.last_source = "hjb-fill"
        self.last_event_wall = time.time()

    def _scatter_offsets(self, times: Deque[float], prices: Deque[float], sides: Deque[str]):
        xs = list(times)
        ys = list(prices)
        colors = ["#2ecc71" if side == "BUY" else "#e74c3c" for side in sides]
        return xs, ys, colors

    def render(self) -> None:
        if not self.times:
            return

        x = list(self.times)
        self.price_line.set_data(x, list(self.mids))
        self.bid_line.set_data(x, list(self.bids))
        self.ask_line.set_data(x, list(self.asks))
        if self.quote_times:
            quote_x = list(self.quote_times)
            self.quote_bid_line.set_data(quote_x, list(self.quote_bids))
            self.quote_ask_line.set_data(quote_x, list(self.quote_asks))

        order_x, order_y, order_colors = self._scatter_offsets(self.order_times, self.order_prices, self.order_sides)
        exec_x, exec_y, exec_colors = self._scatter_offsets(self.exec_times, self.exec_prices, self.exec_sides)

        self.order_scatter.remove()
        self.exec_scatter.remove()
        self.order_scatter = self.ax_price.scatter(order_x, order_y, s=35, marker="^", c=order_colors, label="Orders")
        self.exec_scatter = self.ax_price.scatter(exec_x, exec_y, s=60, marker="o", c=exec_colors, edgecolors="white", linewidths=0.5, label="Executions")

        self.ax_price.relim()
        market_values = list(self.mids) + list(self.bids) + list(self.asks)
        if market_values:
            low = min(market_values)
            high = max(market_values)
            span = max(high - low, max(high * 0.002, 0.01))
            self.ax_price.set_ylim(low - span * 0.15, high + span * 0.15)
        if len(x) > 1:
            self.ax_price.set_xlim(min(x), max(x))

        self.ax_latency.clear()
        source_values = list(self.source_lags_us)
        dashboard_values = list(self.dashboard_lags_us)
        exec_values = list(self.latencies_us)
        e2e_values = list(self.end_to_end_us)
        compute_values = list(self.compute_lags_us)

        if source_values:
            self.ax_latency.plot(range(len(source_values)), source_values, label="Source lag", color="#3498db", linewidth=1.4)
        if dashboard_values:
            self.ax_latency.plot(range(len(dashboard_values)), dashboard_values, label="Dashboard lag", color="#9b59b6", linewidth=1.2)
        if compute_values:
            self.ax_latency.plot(range(len(compute_values)), compute_values, label="HJB compute", color="#16a085", linewidth=1.3)
        if exec_values:
            self.ax_latency.plot(range(len(exec_values)), exec_values, label="Exec latency", color="#e67e22", linewidth=1.2)
        if e2e_values:
            self.ax_latency.plot(range(len(e2e_values)), e2e_values, label="End-to-end", color="#c0392b", linewidth=1.4, linestyle="--")

        self.ax_latency.set_ylabel("Lag / latency (us)")
        self.ax_latency.set_xlabel("Recent samples")
        self.ax_latency.grid(True, axis="y", alpha=0.25)
        if source_values or dashboard_values or exec_values or e2e_values or compute_values:
            max_value = max(source_values + dashboard_values + exec_values + e2e_values + compute_values)
            self.ax_latency.set_ylim(0, max_value * 1.25 + 1.0)
        self.ax_latency.legend(loc="upper right")

        age_ms = (time.time() - self.last_event_wall) * 1000.0
        stats = [
            f"symbol: {self.last_symbol}",
            f"source: {self.last_source}",
            "feed: REAL (Binance)",
            f"compute: {'REAL (HDL HJB)' if self.backend == 'hjb' else 'SIMULATED'}",
            f"execution: {'SIMULATED (fill model)' if self.backend == 'hjb' else 'SIMULATED'}",
            f"source lag: {self.source_lags_us[-1]:,.1f} us" if self.source_lags_us else "source lag: -",
            f"dashboard lag: {self.dashboard_lags_us[-1]:,.1f} us" if self.dashboard_lags_us else "dashboard lag: -",
            f"compute lag: {self.compute_lags_us[-1]:,.1f} us" if self.compute_lags_us else "compute lag: -",
            f"latency cycles: {self.latency_cycles[-1]}" if self.latency_cycles else "latency cycles: -",
            f"pump q: {self.pump_depth} (max {self.pump_max_depth}) dropped: {self.pump_dropped}",
            f"hjb q: {self.hjb_depth} (max {self.hjb_max_depth}) dropped: {self.hjb_dropped}",
            f"ticks: {self.total_ticks}",
            f"orders: {self.total_orders}",
            f"execs: {self.total_execs}",
            f"exec latency: {self.last_latency:,.1f} us",
            f"idle: {age_ms:,.1f} ms",
        ]
        self.status_text.set_text("\n".join(stats))

    def push_quote(self, quote_event: QuoteEvent) -> None:
        if not self._quote_is_sane(quote_event):
            return
        self.quote_times.append(quote_event.sim_end_ts)
        self.quote_bids.append(quote_event.bid)
        self.quote_asks.append(quote_event.ask)
        self.compute_lags_us.append(quote_event.compute_lag_us)
        self.latency_cycles.append(quote_event.latency_cycles)
        self.last_symbol = f"Q{quote_event.quote_id}"
        self.last_source = "hjb-rtl"
        self.last_event_wall = time.time()

    def _quote_is_sane(self, quote_event: QuoteEvent) -> bool:
        if quote_event.mid <= 0:
            return False
        if quote_event.bid <= 0 or quote_event.ask <= 0:
            return False
        bid_dev = abs((quote_event.bid - quote_event.mid) / quote_event.mid)
        ask_dev = abs((quote_event.ask - quote_event.mid) / quote_event.mid)
        return bid_dev <= self.quote_max_deviation_pct and ask_dev <= self.quote_max_deviation_pct

    def set_telemetry(
        self,
        pump_depth: int,
        pump_dropped: int,
        pump_max_depth: int,
        hjb_depth: int,
        hjb_dropped: int,
        hjb_max_depth: int,
    ) -> None:
        self.pump_depth = pump_depth
        self.pump_dropped = pump_dropped
        self.pump_max_depth = pump_max_depth
        self.hjb_depth = hjb_depth
        self.hjb_dropped = hjb_dropped
        self.hjb_max_depth = hjb_max_depth


class EventPump:
    def __init__(self) -> None:
        self.queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=10_000)
        self.stop_requested = threading.Event()
        self.dropped_events = 0
        self.max_depth = 0

    def put(self, event: Optional[Dict[str, Any]]) -> None:
        if self.stop_requested.is_set():
            return
        try:
            self.queue.put_nowait(event)
            self.max_depth = max(self.max_depth, self.queue.qsize())
        except queue.Full:
            self.dropped_events += 1
            # Drop old data rather than blocking the market feed.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(event)
                self.max_depth = max(self.max_depth, self.queue.qsize())
            except queue.Full:
                self.dropped_events += 1
                pass

    def get(self, timeout: float = 0.01) -> Optional[Dict[str, Any]]:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def metrics(self) -> Dict[str, int]:
        return {
            "depth": self.queue.qsize(),
            "dropped": self.dropped_events,
            "max_depth": self.max_depth,
        }


class EventLogger:
    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self.file = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("a", encoding="utf-8")

    def log(self, event: Dict[str, Any]) -> None:
        if self.file is None:
            return
        self.file.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.file.flush()

    def close(self) -> None:
        if self.file is not None:
            self.file.close()


def utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def parse_live_binance_message(symbol: str, stream: str, message: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return None

    payload = data
    if stream == "bookticker":
        bid = float(payload["b"])
        ask = float(payload["a"])
        mid = (bid + ask) / 2.0
        source_ts = float(payload.get("E", 0.0)) / 1000.0
        recv_ts = utc_ts()
        if source_ts <= 0:
            source_ts = recv_ts
        return {
            "kind": "tick",
            "source_ts": source_ts,
            "recv_ts": recv_ts,
            "ts": recv_ts,
            "symbol": symbol.upper(),
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "source": "binance-bookTicker",
        }

    if stream == "trade":
        price = float(payload["p"])
        spread = max(price * 0.0005, 0.01)
        source_ts = float(payload.get("E", 0.0)) / 1000.0
        recv_ts = utc_ts()
        if source_ts <= 0:
            source_ts = recv_ts
        return {
            "kind": "tick",
            "source_ts": source_ts,
            "recv_ts": recv_ts,
            "ts": utc_ts(),
            "symbol": symbol.upper(),
            "mid": price,
            "bid": price - spread / 2.0,
            "ask": price + spread / 2.0,
            "source": "binance-trade",
        }

    return None


def replay_events(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            stream = str(row.get("stream", "")).lower()
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                continue

            if stream == "bookticker":
                bid = float(payload["b"])
                ask = float(payload["a"])
                mid = (bid + ask) / 2.0
                captured_ts = float(row.get("captured_at_ts", 0.0)) if row.get("captured_at_ts") else 0.0
                now_ts = time.time()
                yield {
                    "kind": "tick",
                    "source_ts": captured_ts if captured_ts > 0 else now_ts,
                    "recv_ts": now_ts,
                    "ts": time.time(),
                    "symbol": str(row.get("symbol", "")) or "BINANCE",
                    "mid": mid,
                    "bid": bid,
                    "ask": ask,
                    "source": "replay-bookTicker",
                }
            elif stream == "trade":
                price = float(payload["p"])
                spread = max(price * 0.0005, 0.01)
                captured_ts = float(row.get("captured_at_ts", 0.0)) if row.get("captured_at_ts") else 0.0
                now_ts = time.time()
                yield {
                    "kind": "tick",
                    "source_ts": captured_ts if captured_ts > 0 else now_ts,
                    "recv_ts": now_ts,
                    "ts": time.time(),
                    "symbol": str(row.get("symbol", "")) or "BINANCE",
                    "mid": price,
                    "bid": price - spread / 2.0,
                    "ask": price + spread / 2.0,
                    "source": "replay-trade",
                }


def start_live_feed(pump: EventPump, symbol: str, stream: str) -> WebSocketApp:
    url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@{'bookTicker' if stream == 'bookticker' else 'trade'}"

    def on_message(_: WebSocketApp, message: str) -> None:
        parsed = parse_live_binance_message(symbol=symbol, stream=stream, message=message)
        if parsed is not None:
            pump.put(parsed)

    def on_error(_: WebSocketApp, error: Any) -> None:
        print(f"WebSocket error: {error}")

    def on_close(_: WebSocketApp, code: Any, reason: Any) -> None:
        print(f"WebSocket closed: code={code} reason={reason}")
        pump.put(None)

    app = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)

    def runner() -> None:
        while not pump.stop_requested.is_set():
            app.run_forever(ping_interval=20, ping_timeout=10)
            if not pump.stop_requested.is_set():
                time.sleep(1.0)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return app


def start_replay_feed(pump: EventPump, input_path: Path, replay_speed: float) -> threading.Thread:
    def runner() -> None:
        previous_ts: Optional[float] = None
        for event in replay_events(input_path):
            if pump.stop_requested.is_set():
                break
            current_ts = event["ts"]
            if previous_ts is not None and replay_speed > 0:
                delay = max(current_ts - previous_ts, 0.0) / replay_speed
                time.sleep(min(delay, 0.25))
            previous_ts = current_ts
            pump.put(event)
        pump.put(None)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


def main() -> int:
    parser = argparse.ArgumentParser(description="Live trading/order-execution visualization")
    parser.add_argument("--mode", choices=["live", "replay"], default="live", help="Data source mode")
    parser.add_argument("--backend", choices=["synthetic", "hjb"], default="synthetic", help="Trading backend")
    parser.add_argument("--symbol", default="btcusdt", help="Binance symbol when in live mode")
    parser.add_argument(
        "--stream",
        choices=["bookticker", "trade"],
        default="bookticker",
        help="Binance stream type when in live mode",
    )
    parser.add_argument("--input", default="data/binance_capture.ndjson", help="Replay NDJSON file")
    parser.add_argument("--replay-speed", type=float, default=1.0, help="Replay speed multiplier")
    parser.add_argument("--latency-us", type=int, default=35, help="Simulated execution latency in microseconds")
    parser.add_argument(
        "--spread-threshold-bps",
        type=float,
        default=2.0,
        help="Signal threshold in basis points",
    )
    parser.add_argument("--window", type=int, default=240, help="Rolling point window for chart history")
    parser.add_argument("--max-orders", type=int, default=80, help="Number of recent orders/executions to show")
    parser.add_argument(
        "--quote-max-deviation-pct",
        type=float,
        default=0.25,
        help="Hide HJB quote lines when quote deviates too far from market mid",
    )
    parser.add_argument(
        "--hjb-fill-model",
        choices=["strict", "simulated"],
        default="simulated",
        help="Fill model for HJB backend",
    )
    parser.add_argument(
        "--hjb-fill-probability",
        type=float,
        default=0.35,
        help="Fill probability for HJB simulated fill mode",
    )
    parser.add_argument(
        "--event-log",
        default="",
        help="Optional JSONL path for event logging",
    )
    args = parser.parse_args()

    pump = EventPump()
    dashboard = Dashboard(
        title="Frankline Arithmax FPGA VHDL + C++ Ultra Low Latency MM",
        max_points=args.window,
        max_orders=args.max_orders,
        backend=args.backend,
        quote_max_deviation_pct=args.quote_max_deviation_pct,
    )
    logger = EventLogger(Path(args.event_log) if args.event_log else None)
    strategy = StrategySim(latency_us=args.latency_us, spread_threshold_bps=args.spread_threshold_bps, window=args.window)
    hjb_engine: Optional[HJBEngine] = None
    if args.backend == "hjb":
        hjb_engine = HJBEngine(
            pump,
            fill_model=args.hjb_fill_model,
            fill_probability=args.hjb_fill_probability,
        )

    def stop(*_: Any) -> None:
        pump.stop_requested.set()
        pump.put(None)
        if hjb_engine is not None:
            hjb_engine.stop()
        logger.close()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if args.mode == "live":
        print(f"Starting live Binance feed for {args.symbol.upper()} ({args.stream})")
        start_live_feed(pump, args.symbol, args.stream)
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            raise FileNotFoundError(f"Replay file not found: {input_path}")
        print(f"Replaying captured data from {input_path}")
        start_replay_feed(pump, input_path, args.replay_speed)

    plt.style.use("seaborn-v0_8-darkgrid")
    plt.tight_layout(rect=[0, 0.02, 1, 0.96])

    def update(_: int):
        # Drain the event queue without blocking the UI thread.
        while True:
            event = pump.get(timeout=0.001)
            if event is None:
                break

            logger.log(dict(event))

            source_ts = float(event.get("source_ts", event["ts"]))
            recv_ts = float(event.get("recv_ts", event["ts"]))

            kind = str(event.get("kind", "tick"))
            if kind == "tick":
                tick = MarketTick(
                    source_ts=source_ts,
                    recv_ts=recv_ts,
                    ts=float(event["ts"]),
                    symbol=str(event["symbol"]),
                    mid=float(event["mid"]),
                    bid=float(event["bid"]),
                    ask=float(event["ask"]),
                    source=str(event.get("source", "feed")),
                )
                dashboard.push_tick(tick)

                if args.backend == "synthetic":
                    order = strategy.observe(tick)
                    if order is not None:
                        dashboard.push_order(order)

                    now_ts = time.time()
                    for exec_event in strategy.due_executions(now_ts):
                        dashboard.push_execution(exec_event)
                elif hjb_engine is not None:
                    hjb_engine.submit_tick(tick)
            elif kind == "quote":
                quote = QuoteEvent(
                    quote_id=int(event["quote_id"]),
                    source_ts=float(event["source_ts"]),
                    recv_ts=float(event["recv_ts"]),
                    sim_start_ts=float(event["sim_start_ts"]),
                    sim_end_ts=float(event["sim_end_ts"]),
                    mid=float(event["mid"]),
                    bid=float(event["bid"]),
                    ask=float(event["ask"]),
                    latency_cycles=int(event["latency_cycles"]),
                    latency_ns=float(event["latency_ns"]),
                    volatility=float(event.get("volatility", 0.0)),
                )
                dashboard.push_quote(
                    quote
                )
                dashboard.push_order(
                    OrderEvent(
                        order_id=quote.quote_id * 2 - 1,
                        side="BUY",
                        source_ts=quote.source_ts,
                        recv_ts=quote.recv_ts,
                        signal_ts=quote.sim_start_ts,
                        exec_ts=quote.sim_end_ts,
                        price=quote.bid,
                    )
                )
                dashboard.push_order(
                    OrderEvent(
                        order_id=quote.quote_id * 2,
                        side="SELL",
                        source_ts=quote.source_ts,
                        recv_ts=quote.recv_ts,
                        signal_ts=quote.sim_start_ts,
                        exec_ts=quote.sim_end_ts,
                        price=quote.ask,
                    )
                )
            elif kind == "execution":
                dashboard.push_execution(
                    ExecEvent(
                        order_id=int(event["order_id"]),
                        side=str(event["side"]),
                        source_ts=float(event["source_ts"]),
                        recv_ts=float(event["recv_ts"]),
                        signal_ts=float(event["signal_ts"]),
                        exec_ts=float(event["exec_ts"]),
                        price=float(event["price"]),
                        latency_us=float(event["latency_us"]),
                    )
                )

        pump_metrics = pump.metrics()
        hjb_metrics = hjb_engine.metrics() if hjb_engine is not None else {"depth": 0, "dropped": 0, "max_depth": 0}
        dashboard.set_telemetry(
            pump_depth=int(pump_metrics["depth"]),
            pump_dropped=int(pump_metrics["dropped"]),
            pump_max_depth=int(pump_metrics["max_depth"]),
            hjb_depth=int(hjb_metrics["depth"]),
            hjb_dropped=int(hjb_metrics["dropped"]),
            hjb_max_depth=int(hjb_metrics["max_depth"]),
        )

        dashboard.render()
        return []

    animation = FuncAnimation(
        dashboard.fig,
        update,
        interval=33,
        blit=False,
        cache_frame_data=False,
    )
    _ = animation
    plt.show()
    pump.stop_requested.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

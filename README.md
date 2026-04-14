# IBKR Bollinger Bands Trading Bot

A Python-based algorithmic trading bot that connects to Interactive Brokers (IBKR) and executes a Bollinger Bands mean-reversion strategy in real time.

---

## Overview

This project implements a systematic trading strategy that:

- Streams live market data from IBKR
- Generates trading signals using Bollinger Bands
- Executes trades automatically via the IBKR API
- Applies basic risk management (stop-loss and take-profit)
- Maintains state across sessions with logging and persistence

The bot is designed for **paper trading and educational purposes**, demonstrating end-to-end trading system development.

---

## Strategy

The strategy is a **mean-reversion model** based on Bollinger Bands:

- **Buy (Long)** when price falls below the lower band (oversold)
- **Sell (Short)** when price rises above the upper band (overbought)
- **Exit** when price reverts to the moving average

Additional controls:
- Stop-loss and take-profit thresholds
- Trading session filtering (only active during market hours)
- Position limits and cooldown periods

---

## Features

- Live market data via IBKR API
- Automated order execution (market orders)
- Position and account synchronization
- Stop-loss and take-profit logic
- Persistent bot state (JSON)
- Execution logging (CSV)
- Partial fill handling
- Reconnection handling
- Trading session filtering
- Duplicate signal/order prevention

---

## Tech Stack

- Python
- ib-insync (Interactive Brokers API)
- pandas
- numpy

---

## Project Structure

```
ibkr-bollinger-bot/
│── ib_bollinger_bot.py
│── README.md
│── .gitignore
│── bot_state/
```

---

## Setup

### 1. Install dependencies

```bash
pip install ib-insync pandas numpy
```

---

### 2. Start IBKR paper trading

- Open Trader Workstation (TWS)
- Log into paper trading
- Enable API access:

```
Global Configuration → API → Settings
```

- Enable Socket Clients
- Disable Read-Only API
- Port: 7497

---

### 3. Run the bot

```bash
python ib_bollinger_bot.py
```

---

## Testing

To verify execution without waiting for signals:

- Manually trigger test trades in the script
- Or reduce Bollinger threshold (e.g. num_std = 0.5) for faster signals

---

## Output

The bot generates:

### Terminal logs
- Trade execution events
- Order status updates
- Strategy signals

### Files
- bot_state/state.json → current state
- bot_state/executions.csv → execution log

---

## Example Workflow

1. Bot connects to IBKR  
2. Streams live price data  
3. Calculates Bollinger Bands  
4. Generates signal  
5. Executes trade  
6. Logs execution and updates state  

---

## Limitations

This project is a prototype, not production-ready.

Limitations include:
- Simplified position cost tracking
- No broker-side stop orders (strategy-driven exits)
- Limited risk controls (no max drawdown / daily loss cap)
- No multi-asset support
- No performance metrics (Sharpe, drawdown, etc.)

---

## Future Improvements

- True average-cost position tracking
- Broker-level bracket orders (stop + take profit)
- Backtesting engine integration
- Multi-asset portfolio support
- Performance analytics dashboard
- Cloud deployment (AWS)

---

## Disclaimer

This project is for educational purposes only.

It is not intended for live trading with real capital.  
Use at your own risk.

---

## Author

Vedang Pandit
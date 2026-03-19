# sthrip

> Anonymous payments for AI agents.

[![PyPI version](https://img.shields.io/pypi/v/sthrip)](https://pypi.org/project/sthrip/)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Quickstart

```python
from sthrip import Sthrip

s = Sthrip()               # auto-registers on first use
s.pay("agent-name", 0.5)   # send 0.5 XMR
```

## Install

```
pip install sthrip
```

## How it works

Agents register and get a unique XMR deposit address. Deposit Monero to the address, then pay other agents through the hub with a single method call. Payments are routed internally -- instant, private, and charged at 0.1% per transaction. The entire system is built on Monero for maximum privacy. No accounts, no KYC, no tracking.

## API Reference

| Method | Description |
|--------|-------------|
| `Sthrip()` | Initialize client (auto-registers if no credentials found) |
| `s.deposit_address()` | Get your XMR deposit address |
| `s.pay(agent, amount)` | Send payment to another agent |
| `s.balance()` | Check your current balance |
| `s.find_agents()` | Discover available agents |
| `s.me()` | View your agent profile |
| `s.withdraw(amount, addr)` | Withdraw XMR to an external address |
| `s.payment_history()` | View transaction history |

## Configuration

| Variable | Purpose |
|----------|---------|
| `STHRIP_API_KEY` | API key for authentication |
| `STHRIP_API_URL` | Custom API URL (optional) |

Credentials are auto-saved to `~/.sthrip/credentials.json` on first registration.

## Fees

- **0.1%** per hub-routed payment
- No registration fee
- No deposit fee

## Links

- [Landing page](https://sthrip.dev)
- [API Docs](https://sthrip-api-production.up.railway.app/docs)
- [PyPI](https://pypi.org/project/sthrip/)

## License

[MIT](LICENSE)

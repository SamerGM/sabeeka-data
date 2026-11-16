# sabeeka-data

Public price feed for the **Sabeeka · سبيكة** gold app (Egypt & UAE). Prices only — no app code.

A scheduled GitHub Action (`.github/workflows/feed.yml`, every ~15 min) runs `feed/sabeeka_feed.py`, which:

| Data | Source | Check |
|---|---|---|
| Spot XAU/USD | Gold-API (interim free feed) | vs iGold.ae pure-metal rate |
| USD/EGP, AED/EGP | Central Bank of Egypt | official mid rate |
| USD/AED | Central Bank of the UAE peg 3.6725 | — |
| Egypt gram prices | Gold Division (egajtd.com), fallback El Galla published prices | must be within −3 % / +12 % of spot × CBE rate |
| UAE gram prices | Dubai Gold & Jewellery Group | same parity band |
| Seller products | Gold Era Egypt price list; Emirates / PAMP / Valcambi via iGold.ae | making = list price ÷ weight − gram price |

Output (branch `data`):

```
v1/snapshot.json     https://raw.githubusercontent.com/SamerGM/sabeeka-data/data/v1/snapshot.json
v1/config.json       https://raw.githubusercontent.com/SamerGM/sabeeka-data/data/v1/config.json
v1/history/XAUUSD_{DAY,WEEK,MONTH,YEAR}.json
```

All decimals are JSON strings. Sellers without a public machine-readable price list are published with
`verified: false` and **no figures** (nothing is estimated for them).

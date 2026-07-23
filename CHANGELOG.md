

## V6.0 — 2026-07-22

### Added
- Dead Zone Filter: RSI 40-65 zone where no trades are forced
- Silent action support in JSON parser and execution pipeline
- Volume Anomaly Detector (Karbalaii 2025 methodology)
- Strategy Market: coil/squeeze breakout detection for any asset
- Fear & Greed regime classification (Farzulla 2026 thresholds)
- Proportional trade sizing EUR5-8

### Changed
- min_eur_to_trade: 1.0 → 5.0
- RSI buy threshold: 30 → 35
- min_24h_change_signal: 3% → 5%
- "Never silent" rule removed — silent is valid
- Deterministic rules use CONFIG thresholds

### Fixed
- Ping-pong trading on neutral RSI (22 trades/day with net -EUR1.11)
- Silent action now checked before data validation
- NONE asset no longer flagged as placeholder

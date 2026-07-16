#!/usr/bin/env python3
"""
Augustus Runner — Autonomous trading loop.
===========================================
Replica of the production scheduling system. Runs the orchestrator
and handles logging. Designed to be called from cron.

Production schedule:
  - Sentinel: every 15 min (portfolio watch + dispatch orchestrator)
  - Stop-loss: every 5 min (watchdog)
  - S²-MAD: 4x daily at 9,12,15,21 (hybrid analysis)

Usage:
  python runner.py                    # Full trading run
  python runner.py --mode scan        # Scan only, no trades
  python runner.py --mode stop-loss   # Stop-loss watchdog only

Configuration:
  Set AUGUSTUS_INTERVAL_MINUTES to change the monitoring frequency
  when using the cron wrapper script.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('AUGUSTUS_DATA_DIR', Path.home() / '.augustus'))
LOG_DIR = DATA_DIR / 'logs'
ALERTS_FILE = DATA_DIR / 'sentinel_alerts.json'


def run_orchestrator(mode="trade", regime="auto", sentiment="N/A"):
    """Run the Augustus orchestrator."""
    cmd = [
        sys.executable, "-m", "augustus.orchestrator",
        "--mode", mode,
    ]
    if regime != "auto":
        cmd += ["--regime", regime]
    if sentiment != "N/A":
        cmd += ["--sentiment", sentiment]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                cwd=str(PROJECT_ROOT),
                                env={**os.environ, 'PYTHONPATH': str(PROJECT_ROOT)})
        output = result.stdout.strip()
        if result.returncode != 0:
            print(f"[{datetime.now().strftime('%H:%M')}] ERROR: orchestrator rc={result.returncode}")
            print(result.stderr[:500])
            return None

        # Extract JSON result
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('{'):
                try:
                    return json.loads(line)
                except:
                    pass
        return None
    except subprocess.TimeoutExpired:
        print(f"[{datetime.now().strftime('%H:%M')}] TIMEOUT: orchestrator >60s")
        return None
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M')}] ERROR: {e}")
        return None


def run_stop_loss():
    """Run the stop-loss watchdog."""
    cmd = [sys.executable, str(PROJECT_ROOT / "augustus" / "stop_loss.py"), "--check"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0 and result.stderr.strip():
            print(f"[SL] {result.stderr.strip()[:300]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("[SL] TIMEOUT")
        return False
    except Exception as e:
        print(f"[SL] ERROR: {e}")
        return False


def quick_portfolio_scan():
    """Fast scan: check balances + basic market data without LLM call."""
    try:
        from augustus.kraken_client import get_balance, calculate_portfolio_value

        balances = get_balance()
        if not balances:
            return None

        total, breakdown = calculate_portfolio_value(balances)
        print(f"[{datetime.now().strftime('%H:%M')}] Portfolio: EUR{total:.2f}")
        print(f"  Assets: {len(balances)} | Cash: EUR{balances.get('ZEUR', 0):.2f}")
        return {"total_eur": total, "balances": balances, "breakdown": breakdown}
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M')}] Scan error: {e}")
        return None


def check_urgent_alerts(scan_result):
    """Check for conditions that warrant immediate trading."""
    if not scan_result:
        return []

    alerts = []
    total = scan_result.get('total_eur', 0)
    balances = scan_result.get('balances', {})

    # Cash too high (>50%) - should deploy
    cash = balances.get('ZEUR', 0)
    if total > 0 and cash / total > 0.5:
        alerts.append(f"High cash: EUR{cash:.2f} ({cash/total*100:.0f}%)")

    # Cash too low (<1%) - risk of dust
    if total > 10 and cash < 1.0:
        alerts.append(f"Low cash: EUR{cash:.2f}")

    return alerts


def save_alerts(alerts):
    """Save alerts to sentinel file."""
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_FILE, 'w') as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "alerts": alerts,
        }, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Augustus Runner - Autonomous Trading Loop")
    parser.add_argument("--mode", choices=["full", "scan", "stop-loss", "quick"],
                        default="full",
                        help="Run mode: full (trade), scan (no trade), stop-loss only, quick (balance check)")
    parser.add_argument("--regime", choices=["auto", "bull", "bear"],
                        default="auto",
                        help="Market regime override")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress non-error output")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    start = time.time()

    if args.mode == "stop-loss":
        success = run_stop_loss()
        sys.exit(0 if success else 1)

    if args.mode == "quick":
        quick_portfolio_scan()
        return

    # ─── Full trading run ───────────────────────────────────────────────────
    print(f"═══ Augustus Runner {datetime.now().strftime('%H:%M')} ═══")

    # 1. Quick scan first
    scan = quick_portfolio_scan()
    alerts = check_urgent_alerts(scan) if scan else []

    if alerts:
        print(f"  🚨 Urgent: {', '.join(alerts)}")
        save_alerts(alerts)

    # 2. Run orchestrator (unless scan-only)
    if args.mode == "scan":
        print("  Scan mode - orchestrator skipped")
        run_stop_loss()
        return

    result = run_orchestrator(
        mode="scan" if args.mode == "scan" else "trade",
        regime=args.regime,
    )

    # 3. Stop-loss watchdog (always, after orchestrator)
    run_stop_loss()

    elapsed = time.time() - start
    action = result.get('action', 'unknown') if result else 'error'
    cost = result.get('cost', 0) if result else 0

    if not args.quiet or action != 'silent':
        print(f"  Done: {action} | ${cost:.5f} | {elapsed:.1f}s")

    # Log
    log_file = LOG_DIR / f"runner_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_file, 'w') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "cost": cost,
            "elapsed_s": round(elapsed, 1),
            "scan": scan,
            "orchestrator": result,
        }, f, indent=2, default=str)


if __name__ == "__main__":
    main()

#!/bin/bash
# Augustus Stop-Loss Watchdog
# Corre de 5 em 5 minutos para verificar e executar stops
# Silencio se nada a reportar
cd /root
output=$(python3 /root/.hermes/scripts/stop_loss_manager.py --check 2>&1)
exit_code=$?
if [ $exit_code -ne 0 ]; then
    echo "[SL-WATCHDOG] ERRO: $output"
    exit 1
fi
if echo "$output" | grep -q "STOP ATINGIDO\|VENDA EXECUTADA"; then
    echo "$output"
fi
exit 0

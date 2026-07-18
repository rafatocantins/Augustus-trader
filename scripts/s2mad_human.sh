#!/bin/bash
# Augustus S2-MAD — output em linguagem humana, sem JSON
cd /root
python3 /root/.hermes/scripts/augustus_orchestrator_v4.py --mode trade 2>&1 | python3 -c "
import sys, re

lines = sys.stdin.readlines()
summary = []
in_json = False
json_depth = 0
decision = None

for line in lines:
    s = line.strip()
    
    # Detetar inicio do JSON (a primeira chave)
    if s == '{' and not in_json:
        in_json = True
        json_depth = 1
        continue
    
    # Seguir profundidade do JSON
    if in_json:
        json_depth += s.count('{') - s.count('}')
        if json_depth <= 0:
            in_json = False
        continue
    
    # Extrair decisao se existir (linha com 'decision' ou 'trading_decision')
    if 'trading_decision' in s.lower() or '\"decision\"' in s.lower():
        # Tentar extrair valor entre aspas
        m = re.search(r':\s*\"(.+?)\"', s)
        if m:
            decision = m.group(1)
            # Des-escapar unicode
            decision = decision.encode().decode('unicode_escape', errors='ignore')
            decision = decision.replace('\\\\n', '\n')
    
    # Capturar linhas de resumo (comecam com emoji)
    if s and (s[0] in '💡📤📥🔵🟢🔴💰📈📉🌍💸🎯⚠✅❌🟡🔥' or s.startswith('===') or s.startswith('Custo') or s.startswith('Regime')):
        summary.append(s)

# Output final
if summary:
    for line in summary:
        print(line)
elif decision:
    print(decision[:400])
else:
    # Fallback: ultimas 8 linhas
    for line in lines[-8:]:
        print(line.strip())
"

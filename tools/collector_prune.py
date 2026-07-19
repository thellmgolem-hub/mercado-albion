#!/usr/bin/env python
"""Poda DIÁRIA do banco — roda 1×/dia no GitHub Actions (job separado do sweep).

O sweep (tools/collector_tick.py) desliga a poda por-tick (ALBION_SWEEP_NO_PRUNE)
porque cada job do Actions é um processo novo e a poda rodaria a cada job. Aqui
ela roda UMA vez por dia: agrega snapshots antigos (snapshot_prune) e apaga
histórico além da janela (history_prune) — mantém o Postgres free (500 MB) magro.

Uso (o workflow faz isto):
    DATABASE_URL=postgresql://...:6543/postgres  python tools/collector_prune.py
"""
import os
import sys

os.environ["ALBION_NO_AUTOCOLLECT"] = "1"
os.environ["ALBION_NO_WATCHDOG"] = "1"
os.environ.pop("DISCORD_BOT_TOKEN", None)
os.environ.pop("ALBION_BOOTSTRAP_ADMIN", None)

if not os.environ.get("DATABASE_URL", "").strip():
    sys.exit("ERRO: DATABASE_URL não definido.")

import app  # noqa: E402


def main() -> int:
    print(f"[poda] db={app._db_size_mb_cached()} MB antes", flush=True)
    try:
        print(f"[poda] snapshot_prune: {app.aodp.snapshot_prune(vacuum=False)}",
              flush=True)
    except Exception as exc:
        print(f"[poda] snapshot_prune falhou: {exc!r}", flush=True)
    try:
        print(f"[poda] history_prune: {app.aodp.history_prune()}", flush=True)
    except Exception as exc:
        print(f"[poda] history_prune falhou: {exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

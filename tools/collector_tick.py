#!/usr/bin/env python
"""Coletor de mercado FORA do app — feito para o GitHub Actions (banda grátis).

Por que existe: no plano free do Render a BANDA de saída é 5 GB/mês e o coletor
sozinho baixava ~25 GB/mês da AODP → suspensão (jul/2026). A cura é rodar o
download onde a banda é grátis e ilimitada (GitHub Actions), gravando direto no
Postgres do Supabase. O Render passa a só SERVIR (banda ~0).

Como: reaproveita EXATAMENTE a lógica do app (`app._sweep_tick_core`), que já é
dual (SQLite/Postgres) e concorrência-segura (`sweep_reserve` = cursor atômico).
Importar `app` NÃO liga bot/vigia/coletor — eles só sobem sob uvicorn (_lifespan);
no import o app apenas conecta o banco via DATABASE_URL. Ver docs no topo de app.py.

Uso (o workflow faz isto):
    DATABASE_URL=postgresql://...:6543/postgres  python tools/collector_tick.py

Env:
    DATABASE_URL            (obrigatório) string do pooler do Supabase (porta 6543)
    COLLECTOR_BUDGET_SEC    segundos de trabalho por job (padrão 480 = 8 min)
    COLLECTOR_BATCH         itens por tick (padrão 200)
"""
import os
import sys
import time

# --- Guardas ANTES de importar o app (o import já conecta o banco) -----------
# Nunca ligar bot/vigia/coletor-local/bootstrap neste processo efêmero.
os.environ["ALBION_NO_AUTOCOLLECT"] = "1"
os.environ["ALBION_NO_WATCHDOG"] = "1"
os.environ["ALBION_SWEEP_NO_PRUNE"] = "1"   # poda roda em job dedicado 1×/dia
os.environ.pop("DISCORD_BOT_TOKEN", None)   # sem gateway do Discord aqui
os.environ.pop("ALBION_BOOTSTRAP_ADMIN", None)

if not os.environ.get("DATABASE_URL", "").strip():
    sys.exit("ERRO: DATABASE_URL não definido — este coletor só faz sentido "
             "apontando para o Postgres do Supabase.")

import app  # noqa: E402  (conecta o banco no import; não sobe bot/vigia)

BUDGET = int(os.environ.get("COLLECTOR_BUDGET_SEC", "480"))
BATCH = int(os.environ.get("COLLECTOR_BATCH", "200"))


def main() -> int:
    backend = getattr(app.aodp.db, "backend", "sqlite")
    print(f"[coletor] backend={backend} budget={BUDGET}s batch={BATCH}",
          flush=True)
    fim = time.time() + BUDGET
    ticks = 0
    last = {}
    while time.time() < fim:
        last = app._sweep_tick_core(BATCH)
        ticks += 1
        prog = last.get("progress_pct")
        print(f"[coletor] tick {ticks}: cursor={last.get('from_cursor')} "
              f"took={last.get('took')} preços={last.get('price_rows')} "
              f"hist={last.get('history_series')} modo={last.get('mode')} "
              f"prog={prog}% db={last.get('db_mb')}MB", flush=True)
        if last.get("paused"):
            print(f"[coletor] autolimite pausou a coleta: {last.get('paused')}",
                  flush=True)
            break
        if prog == 100.0:                     # deu a volta no universo — pode parar
            print("[coletor] ciclo completo do universo.", flush=True)
            break
        time.sleep(1)                         # respiro (o client já tem Throttle)
    # 1 toque de killboard por job (idempotente via checkpoint)
    try:
        intel = app._intel_tick_core()
        print(f"[coletor] intel: {intel}", flush=True)
    except Exception as exc:                  # nunca derruba o job por causa do intel
        print(f"[coletor] intel falhou (segue): {exc!r}", flush=True)
    print(f"[coletor] fim — {ticks} ticks.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

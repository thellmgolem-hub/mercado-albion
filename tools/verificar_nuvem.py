# -*- coding: utf-8 -*-
"""Verifica a plataforma na nuvem de FORA e explica o estado em português.

Pra que serve: saber se está tudo de pé ANTES da guilda perceber (a doutrina
"sabe antes da guilda"). Lê o /api/health, que é PÚBLICO — não precisa de login,
senha nem token. Roda do seu PC, de qualquer lugar.

    python tools/verificar_nuvem.py
    python tools/verificar_nuvem.py https://mercado-albion.onrender.com

A URL sai de (nesta ordem): argumento, ALBION_PUBLIC_URL, RENDER_EXTERNAL_URL,
ou o padrão abaixo.

Código de saída: 0 = tudo certo · 1 = tem problema · 2 = não respondeu.
Assim dá pra usar em cron/monitor ("&& echo ok").
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL_PADRAO = "https://mercado-albion.onrender.com"
TIMEOUT_S = 30            # free tier dormido leva ~30-60s pra acordar


def _buscar(url: str):
    """(dados, erro) do /api/health. Nunca levanta — o erro vira texto PT."""
    alvo = url.rstrip("/") + "/api/health"
    req = urllib.request.Request(alvo, headers={"User-Agent": "albion-verificador"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        corpo = ""
        try:
            corpo = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code in (402, 403) or "suspend" in corpo.lower():
            return None, ("A plataforma parece SUSPENSA pelo Render "
                          f"(HTTP {e.code}). Cheque a cota no painel.")
        return None, f"O servidor respondeu HTTP {e.code}. {corpo}".strip()
    except urllib.error.URLError as e:
        return None, (f"Não consegui falar com {url} ({e.reason}). "
                      "Ou está fora do ar, ou suspenso, ou a URL está errada.")
    except Exception as e:                       # timeout, JSON quebrado, etc.
        return None, (f"Sem resposta de {url} em {TIMEOUT_S}s ({type(e).__name__}). "
                      "No free tier isso costuma ser sono profundo ou suspensão.")


def _minutos(seg):
    if seg is None:
        return "?"
    seg = float(seg)
    if seg < 90:
        return f"{seg:.0f}s"
    if seg < 5400:
        return f"{seg / 60:.0f}min"
    return f"{seg / 3600:.1f}h"


def _linha(ok, titulo, detalhe=""):
    marca = "OK  " if ok else ("??  " if ok is None else "FALHA")
    print(f"  [{marca:5}] {titulo}" + (f" — {detalhe}" if detalhe else ""))


def avaliar(d: dict):
    """Imprime o diagnóstico e devolve a lista de problemas encontrados."""
    problemas = []

    # --- banco ---
    if d.get("db_ok"):
        _linha(True, "Banco de dados", f"respondendo ({d.get('backend', '?')})")
    else:
        _linha(False, "Banco de dados", "NÃO respondeu")
        problemas.append(
            "O banco não respondeu. No Supabase free, a causa mais comum é o "
            "projeto PAUSADO por 7 dias sem uso — abra o painel e clique em "
            "Restore/Resume. Se não for isso, veja se o disco encheu.")

    # --- coleta (o que mantém os preços atuais) ---
    idade = d.get("sweep_age_s")
    if d.get("coleta_ok") and d.get("coletor_externo_ok"):
        _linha(True, "Coleta de preços", f"último ciclo há {_minutos(idade)}")
    elif d.get("coletor_externo_ok") is False:
        _linha(False, "Coletor do GitHub Actions", f"parado há {_minutos(idade)}")
        problemas.append(
            "O coletor externo (GitHub Actions) parou. Confira em Actions se o "
            "workflow 'coletor-mercado' está rodando e se a variável "
            "COLETOR_ATIVO=1 e o secret DATABASE_URL existem. Sem ele os preços "
            "envelhecem e o Supabase pode pausar por inatividade.")
    else:
        _linha(False, "Coleta de preços", f"sem ciclo recente ({_minutos(idade)})")
        problemas.append("A coleta não rodou no tempo esperado.")

    # --- killboard ---
    if d.get("intel_ok"):
        idade_i = d.get("intel_age_s")
        _linha(True, "Killboard (Mural/Demanda)",
               f"há {_minutos(idade_i)}" if idade_i is not None
               else "sem ciclo registrado ainda")
    else:
        _linha(False, "Killboard", "atrasado")
        problemas.append(
            "O killboard não atualiza. O Mural de Conquistas e as análises de "
            "demanda ficam desatualizados (não derruba o resto).")

    # --- disco ---
    modo, mb = d.get("db_mode"), d.get("db_mb")
    if not d.get("db_measure_ok"):
        _linha(None, "Disco do banco", "não consegui medir (assumindo o pior, por segurança)")
        problemas.append(
            "Não deu pra medir o tamanho do banco. Por segurança o app corta o "
            "histórico. Vale olhar o painel do Supabase.")
    elif modo == "ok":
        _linha(True, "Disco do banco", f"{mb} MB (folgado)")
    elif modo == "soft":
        _linha(None, "Disco do banco", f"{mb} MB — cortando histórico pra não encher")
        problemas.append(
            f"O banco passou do limite macio ({mb} MB): o app já está podando "
            "sozinho. Não é queda, mas o histórico fica mais curto.")
    else:
        _linha(False, "Disco do banco", f"{mb} MB — coleta PAUSADA")
        problemas.append(
            f"O banco chegou no limite duro ({mb} MB) e a coleta está pausada "
            "(leitura segue). Precisa liberar espaço no Supabase.")

    # --- bot do Discord ---
    if d.get("bot_ok"):
        rs = d.get("bot_restarts") or 0
        _linha(True, "Bot do Discord", "conectado" + (f" ({rs} reinícios)" if rs else ""))
        if rs and rs >= 3:
            problemas.append(
                f"O bot reiniciou {rs} vezes. Costuma ser instabilidade de rede "
                "ou token; se subir muito, olhe o log do Render.")
    elif d.get("bot_ok") is False:
        _linha(False, "Bot do Discord", "fora")
        problemas.append(
            "O bot não está conectado — os comandos no Discord não respondem. "
            "Enquanto isso, dá pra subir o bot no seu PC (modo ponte): "
            "python tools/discord_bot.py")
    else:
        _linha(None, "Bot do Discord", "sem informação")

    # --- vigia (só informativo) ---
    vigia = d.get("vigia")
    if vigia and vigia != "desligado":
        _linha(None, "Vigia da coleta", f"{vigia} (assumiu a coleta {d.get('vigia_takeovers', 0)}x)")

    up = d.get("uptime_s")
    if up is not None:
        _linha(True, "No ar há", _minutos(up))

    return problemas


def main():
    url = (sys.argv[1] if len(sys.argv) > 1 else
           os.environ.get("ALBION_PUBLIC_URL")
           or os.environ.get("RENDER_EXTERNAL_URL") or URL_PADRAO).strip()
    print(f"\nVerificando {url} ...\n")
    dados, erro = _buscar(url)
    if dados is None:
        print(f"  [FALHA] {erro}\n")
        print("O que fazer agora:")
        print("  1. Painel do Render: o serviço está 'Suspended' ou dormindo?")
        print("     (se estiver suspenso por cota, ela reseta no início do ciclo)")
        print("  2. Painel do Supabase: o projeto está pausado? Clique em Restore.")
        print("  3. Enquanto isso a guilda não fica sem nada — suba o bot no PC:")
        print("     python tools/discord_bot.py   (modo ponte, sem plataforma)\n")
        return 2

    print("Estado da plataforma:\n")
    problemas = avaliar(dados)
    geral = dados.get("ok")
    print()
    if geral and not problemas:
        print("TUDO CERTO. A plataforma está de pé e se mantendo sozinha.\n")
        return 0
    print("PRECISA DE ATENÇÃO:\n")
    for i, p in enumerate(problemas, 1):
        print(f"  {i}. {p}")
    if geral and problemas:
        print("\n(O app se diz saudável no geral, mas os pontos acima merecem olhada.)")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())

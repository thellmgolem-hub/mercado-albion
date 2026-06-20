# -*- coding: utf-8 -*-
"""Sobe o app LOCALMENTE sem login — para testar à vontade na própria máquina.

    python tools/run_local.py

Equivale a `python app.py`, mas com a autenticação DESLIGADA
(ALBION_AUTH_DISABLED=1) — sem tela de acesso, sem vínculo de dispositivo.
Ideal para explorar a plataforma sozinho. NÃO use em rede/produção: a nuvem
(Render) sempre roda com auth ligada.
"""
import os
os.environ.setdefault("ALBION_AUTH_DISABLED", "1")

import threading
import webbrowser

import uvicorn

from app import app, HOST, PORT  # noqa: E402  (env tem de vir antes do import)

if __name__ == "__main__":
    threading.Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    print(f"Mercado Albion (LOCAL, sem login) — http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

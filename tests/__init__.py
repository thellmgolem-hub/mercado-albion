# -*- coding: utf-8 -*-
"""Pacote de testes.

Desliga a autenticação ANTES de qualquer módulo de teste importar
albion.config. O flag é lido UMA vez no import de config
(AUTH_REQUIRED = os.environ.get("ALBION_AUTH_DISABLED") != "1"); sem isto, a
ordem de descoberta importava — test_advisor carrega albion.config antes de
test_core setar a env, deixando a suíte inteira com auth LIGADA e os testes de
endpoint (sem credencial) davam 503. Como este __init__ roda antes de todo
tests.test_*, a env fica garantida e a suíte passa a ser order-independent.
"""
import os

os.environ.setdefault("ALBION_AUTH_DISABLED", "1")

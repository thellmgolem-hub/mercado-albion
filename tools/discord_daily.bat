@echo off
rem ============================================================================
rem discord_daily.bat — publica o relatorio diario + digest de laborers no
rem Discord via webhook. Template p/ o Agendador de Tarefas do Windows.
rem
rem COMO CONFIGURAR O WEBHOOK:
rem   1. No Discord: Configuracoes do canal > Integracoes > Webhooks >
rem      Novo webhook > Copiar URL.
rem   2. Defina a variavel de ambiente ALBION_DISCORD_WEBHOOK com essa URL:
rem      - permanente (recomendado):  setx ALBION_DISCORD_WEBHOOK "https://discord.com/api/webhooks/..."
rem        (abra um NOVO terminal depois do setx — ele nao afeta a sessao atual)
rem      - ou descomente e preencha a linha "set" abaixo (fica no .bat; nao
rem        commite o arquivo com a URL preenchida).
rem
rem COMO AGENDAR (rodar 1x, num prompt como o usuario que vai executar):
rem   schtasks /Create /TN "Albion Discord Daily" /TR "\"D:\analise de mercado albion\tools\discord_daily.bat\"" /SC DAILY /ST 09:00
rem   (ajuste o caminho se o repo estiver em outra pasta; /ST = horario local)
rem   Para testar na hora:      schtasks /Run /TN "Albion Discord Daily"
rem   Para remover a tarefa:    schtasks /Delete /TN "Albion Discord Daily" /F
rem ============================================================================

rem Console Windows usa cp1252 — força UTF-8 no Python p/ nao quebrar acentos.
set PYTHONIOENCODING=utf-8

rem Descomente e preencha p/ definir o webhook aqui (em vez do setx):
rem set ALBION_DISCORD_WEBHOOK=https://discord.com/api/webhooks/SEU/WEBHOOK

rem Vai para a raiz do repo (este .bat mora em tools\).
cd /d "%~dp0.."

rem Relatorio do dia (recomendacoes, movers, backtest, cobertura).
python analyze.py report --discord

rem Digest dos trabalhadores de fabricacao (top 8, encher craftando).
python analyze.py digest --view laborers --discord

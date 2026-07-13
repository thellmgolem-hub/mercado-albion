@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ============================================================
echo   RECUPERAR SENHA DO ADMIN - Mercado Albion (nuvem)
echo ============================================================
echo.
echo PASSO 1: no Render (aba Environment), clique no OLHO da linha
echo          DATABASE_URL e copie o valor inteiro.
echo.
echo PASSO 2: cole a URL aqui embaixo (clique com o botao direito
echo          do mouse para colar) e tecle Enter:
echo.
set /p DATABASE_URL=URL:
echo.
python tools\reset_cloud_admin.py
echo.
echo (Anote a SENHA que apareceu acima. Ja pode fechar esta janela.)
pause
endlocal

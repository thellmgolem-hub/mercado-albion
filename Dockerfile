# Imagem para o Hugging Face Spaces (SDK Docker) — hospedagem GRÁTIS, sem cartão,
# 2 vCPU / 16 GB RAM, URL HTTPS pública. Roda o MESMO app.py: uvicorn na porta
# 7860 (padrão do HF) e o bot Discord embarcado (sobe no _lifespan). O download
# pesado de mercado NÃO roda aqui — fica no GitHub Actions (tools/collector_tick.py).
#
# Requisitos do HF: rodar como usuário não-root uid 1000 e escutar em 7860.
# DATABASE_URL no HF deve usar o SESSION POOLER do Supabase (porta 5432) — o HF
# só libera saída em 443/5432; a 6543 (transaction pooler) é bloqueada.
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=7860 \
    ALBION_NO_AUTOCOLLECT=1
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

EXPOSE 7860
# uvicorn segura o processo e responde na 7860 (health check do HF); o bot sobe
# como task no mesmo event loop (lifespan). --no-access-log: não loga querystring.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860} --no-access-log"]

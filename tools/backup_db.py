# -*- coding: utf-8 -*-
"""Backup online do cache SQLite (data/cache.db) — seguro com WAL quente.

Usa a API de backup do sqlite3 (con.backup), que tira um retrato consistente
do banco MESMO com o servidor/coletor escrevendo ao mesmo tempo — nunca copie
o arquivo .db na mão com o WAL quente (a cópia sai corrompida/incompleta).

O retrato é comprimido para backups/cache-YYYYMMDD-HHMM.db.zip e a rotação
mantém só os 7 zips mais recentes (os demais são apagados). Sem dependências
fora da stdlib. O banco tem ~1,4 GB, então a cópia + compressão pode levar
alguns minutos.

Uso manual:

    python tools/backup_db.py

Agendamento no Windows (Agendador de Tarefas via schtasks), ex.: todo dia
às 03h30 — ajuste o caminho do python se usar venv:

    schtasks /create /tn "AlbionBackupCache" /sc daily /st 03:30 ^
      /tr "python \"D:\\análise de mercado albion\\tools\\backup_db.py\""

Para conferir/remover:  schtasks /query /tn AlbionBackupCache
                        schtasks /delete /tn AlbionBackupCache /f
"""
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "cache.db"
BACKUP_DIR = ROOT / "backups"
KEEP = 7  # quantos zips manter na rotação


def main() -> int:
    if not DB_PATH.exists():
        print(f"ERRO: banco não encontrado em {DB_PATH}")
        return 2

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    tmp_db = BACKUP_DIR / f"cache-{stamp}.db"
    zip_path = BACKUP_DIR / f"cache-{stamp}.db.zip"

    # 1) retrato consistente via API de backup (aguenta escrita concorrente)
    print(f"copiando {DB_PATH.name} ({DB_PATH.stat().st_size / 1e9:.2f} GB) "
          f"-> {tmp_db.name} ...")
    if tmp_db.exists():  # sobra de execução interrompida
        tmp_db.unlink()
    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(tmp_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # 2) comprime e descarta o .db intermediário
    print(f"comprimindo -> {zip_path.name} ...")
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            zf.write(tmp_db, arcname="cache.db")
    except BaseException:
        zip_path.unlink(missing_ok=True)  # não deixa zip pela metade
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    print(f"backup OK: {zip_path}  ({zip_path.stat().st_size / 1e6:.1f} MB)")

    # 3) rotação: mantém os KEEP mais recentes (o timestamp no nome ordena)
    zips = sorted(BACKUP_DIR.glob("cache-*.db.zip"), reverse=True)
    for old in zips[KEEP:]:
        old.unlink()
        print(f"rotação: removido {old.name}")
    print(f"rotação: {min(len(zips), KEEP)} backup(s) mantido(s) em {BACKUP_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

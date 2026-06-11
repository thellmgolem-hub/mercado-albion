# -*- coding: utf-8 -*-
"""Backup seguro do cache.db (API de backup do SQLite, compatível com WAL).

Gera backups/cache-AAAAMMDD-HHMM.zip e mantém os últimos N (padrão 14).
Uso:  python scripts/backup_cache.py [--keep N]
Agendável pelo Agendador de Tarefas (ver scripts/backup_task.cmd).
"""
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "cache.db"
DEST_DIR = ROOT / "backups"
KEEP = int(sys.argv[sys.argv.index("--keep") + 1]) if "--keep" in sys.argv else 14


def main():
    if not SRC.exists():
        print(f"cache nao encontrado: {SRC}")
        return 1
    DEST_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    tmp = DEST_DIR / f"cache-{stamp}.db"
    zpath = DEST_DIR / f"cache-{stamp}.zip"

    src = sqlite3.connect(str(SRC))
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)  # cópia consistente mesmo com escritas em andamento
    finally:
        dst.close()
        src.close()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "cache.db")
    tmp.unlink()

    old = sorted(DEST_DIR.glob("cache-*.zip"))[:-KEEP]
    for f in old:
        f.unlink()
    print(f"backup ok: {zpath.name} ({zpath.stat().st_size / 1e6:.1f} MB);"
          f" mantidos {min(KEEP, len(sorted(DEST_DIR.glob('cache-*.zip'))))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

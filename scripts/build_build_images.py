# -*- coding: utf-8 -*-
"""Gera as imagens de loadout das builds (web/builds/*.png) e anota o caminho
em data/builds.json (campo `image`).

Roda LOCALMENTE (Windows) — baixa os ícones do render.albiononline.com pelo
mesmo truque do proxy do app (PowerShell/Schannel contorna o Cloudflare) e
compõe a imagem com Pillow. Como o resultado é ESTÁTICO, a nuvem só serve o
PNG pronto (não precisa baixar ícone em runtime, onde não há PowerShell).

Uso:  python scripts/build_build_images.py
"""
import json
import re
import subprocess
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ICONS = DATA / "icons"
OUT = ROOT / "web" / "builds"
ICONS.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
SLOTS = [("weapon", "Arma"), ("offhand", "Off"), ("head", "Cabeça"),
         ("chest", "Peito"), ("shoes", "Pés"), ("cape", "Capa"),
         ("potion", "Poção"), ("food", "Comida")]
SLOT_LABEL = dict(SLOTS)
# Posições no "boneco de equipamento" (igual à tela do jogo): coluna central
# Cabeça -> Peito -> Pés; arma à ESQUERDA do peito, off-hand à DIREITA; capa no
# topo-direito; poção e comida na base. Grade 3 colunas × 4 linhas.
POS = {
    "head":    (1, 0),
    "cape":    (2, 0),
    "weapon":  (0, 1),
    "chest":   (1, 1),
    "offhand": (2, 1),
    "shoes":   (1, 2),
    "potion":  (0, 3),
    "food":    (2, 3),
}


def download_icon(item_id, size=128):
    dest = ICONS / f"{item_id}_q0_s{size}.png"
    if dest.exists():
        return dest
    url = f"https://render.albiononline.com/v1/item/{quote(item_id)}.png?size={size}"
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=15,
                      follow_redirects=True)
        if r.status_code == 200 and r.content[:4] == b"\x89PNG":
            dest.write_bytes(r.content)
            return dest
    except httpx.HTTPError:
        pass
    tmp = dest.with_suffix(".tmp.png")
    for attempt in range(3):        # rede instável / rajada Cloudflare: 3 tentativas
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "[Net.ServicePointManager]::SecurityProtocol = "
                 "[Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                 f"Invoke-WebRequest -Uri '{url}' -OutFile '{tmp}' "
                 f"-UserAgent '{_UA}' -TimeoutSec 25 -UseBasicParsing"],
                capture_output=True, timeout=40)
            if tmp.exists() and tmp.read_bytes()[:4] == b"\x89PNG":
                tmp.replace(dest)
                return dest
        except (subprocess.SubprocessError, OSError):
            pass
        finally:
            tmp.unlink(missing_ok=True)
        time.sleep(0.6 * (attempt + 1))
    return None


def font(size, bold=False):
    names = (["arialbd.ttf", "seguisb.ttf"] if bold else ["arial.ttf", "segoeui.ttf"])
    paths = [f"C:/Windows/Fonts/{n}" for n in names] + [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def slugify(build):
    s = "-".join([build.get("tree", ""), build.get("buildName", ""),
                  build.get("content", "")]).lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def compose(build):
    """Monta a imagem no formato "boneco" (paper-doll) da tela de equipamento do
    jogo: cada slot na sua posição, com moldura de célula. Slots sem item viram
    célula-fantasma rotulada (Cabeça/Peito/Pés...), como no inventário."""
    items = build.get("items") or {}
    cols, rows, cell, ic, top, margin = 3, 4, 108, 78, 54, 14
    W = cols * cell + 2 * margin
    H = top + rows * cell + 10
    img = Image.new("RGBA", (W, H), (32, 34, 37, 255))
    d = ImageDraw.Draw(img)
    tree = build.get("tree", "").replace("Cajados ", "").split(" (")[0]
    # título em PT (buildName_pt); cai pro EN só se faltar
    nome = build.get("buildName_pt") or build.get("buildName", "")
    d.text((margin, 8), nome, fill=(201, 162, 75, 255), font=font(20, bold=True))
    d.text((margin, 32), f"{tree} · {build.get('content', '')}",
           fill=(170, 170, 170, 255), font=font(12))
    fl, fslot = font(11), font(10)
    miss = 0
    for slot, (col, row) in POS.items():
        cx = margin + col * cell
        cy = top + row * cell
        box = [cx + 4, cy + 4, cx + cell - 4, cy + cell - 4]
        it = items.get(slot)
        d.rounded_rectangle(
            box, radius=9,
            fill=(48, 50, 55, 255) if it else (40, 42, 46, 255),
            outline=(201, 162, 75, 130) if it else (60, 62, 66, 255), width=2)
        if not it:                       # célula-fantasma: só o rótulo do slot
            lbl = SLOT_LABEL.get(slot, "")
            w = d.textlength(lbl, font=fslot)
            d.text((cx + (cell - w) / 2, cy + cell / 2 - 6), lbl,
                   fill=(96, 98, 102, 255), font=fslot)
            continue
        p = download_icon(it["id"])
        drawn = False
        if p:
            try:
                icon = Image.open(p).convert("RGBA").resize((ic, ic))
                img.alpha_composite(icon, (cx + (cell - ic) // 2, cy + 6))
                drawn = True
            except Exception:
                pass
        if not drawn:
            miss += 1
        name = (it.get("pt_clean") or "")[:16]
        w = d.textlength(name, font=fl)
        d.text((cx + (cell - w) / 2, cy + 6 + ic + 1), name,
               fill=(220, 220, 220, 255), font=fl)
    return img.convert("RGB"), miss


def main():
    data = json.loads((DATA / "builds.json").read_text(encoding="utf-8"))
    builds = data["builds"]
    total_miss = 0
    for b in builds:
        img, miss = compose(b)
        total_miss += miss
        name = slugify(b) + ".png"
        img.save(OUT / name, "PNG")
        b["image"] = f"/builds/{name}"
    (DATA / "builds.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(builds)} imagens geradas em web/builds/ "
          f"(ícones faltando: {total_miss})")


if __name__ == "__main__":
    main()

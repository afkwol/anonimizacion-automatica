"""Renderiza original + anonimizado de un PDF a PNGs en /tmp/anonrender/<stem>/."""
import sys
from pathlib import Path
import fitz

LAB = Path(__file__).parent / "tanda1"
OUT = Path("/tmp/anonrender")

def render(stem: str, dpi: int = 90) -> Path:
    target = OUT / stem
    target.mkdir(parents=True, exist_ok=True)
    for kind, path in [("orig", LAB / f"{stem}.pdf"),
                       ("anon", LAB / f"{stem}_anonimizado.pdf")]:
        with fitz.open(str(path)) as d:
            for i, page in enumerate(d):
                p = target / f"{kind}_{i:03d}.png"
                if not p.exists():
                    pix = page.get_pixmap(dpi=dpi)
                    pix.save(str(p))
    return target

if __name__ == "__main__":
    for stem in sys.argv[1:]:
        out = render(stem)
        print(f"OK {stem} -> {out}")

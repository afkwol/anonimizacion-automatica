"""Para cada par orig/anon detecta páginas 'interesantes':
- changed: el texto difiere (hubo anonimización)
- orphan: aparece apellido de una parte sin tachar (debió anonimizarse y no se hizo)

Sólo las interesantes se renderizan y devuelven para inspección visual.
"""
from __future__ import annotations
import json, re, unicodedata, sys
from pathlib import Path
import fitz

LAB = Path(__file__).parent / "tanda1"
OUT = Path("C:/tmp/anonrender")


def remove_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def party_surname(party_name: str) -> str:
    """Devuelve el apellido (token más significativo) de la parte."""
    if "," in party_name:
        return party_name.split(",")[0].strip().split()[-1]
    words = party_name.split()
    if not words:
        return ""
    return max(words, key=len)


def party_full_tokens(party_name: str) -> set[str]:
    """Tokens de >=4 chars del nombre completo."""
    no_acc = remove_accents(party_name.lower())
    return {t for t in re.findall(r"\b[a-zñ]{4,}\b", no_acc)}


def inspect(stem: str) -> dict:
    orig_p = LAB / f"{stem}.pdf"
    anon_p = LAB / f"{stem}_anonimizado.pdf"
    audit_p = LAB / f"{stem}_audit.json"
    if not (orig_p.exists() and anon_p.exists() and audit_p.exists()):
        return {"stem": stem, "error": "missing files"}

    audit = json.loads(audit_p.read_text(encoding="utf-8"))
    parties = audit.get("parties", [])

    # Tokens significativos esperados (apellidos, nombres largos, etc.)
    party_tokens: dict[str, set[str]] = {p["nombre"]: party_full_tokens(p["nombre"]) for p in parties}
    # Apellidos individuales — los buscamos sueltos como "orphan check" (saber dónde aparecen)
    surnames = {party_surname(p["nombre"]).lower() for p in parties if party_surname(p["nombre"])}
    surnames_no_acc = {remove_accents(s) for s in surnames if len(s) >= 4}

    with fitz.open(str(orig_p)) as do, fitz.open(str(anon_p)) as da:
        n_pages = min(len(do), len(da))
        per_page = []
        for i in range(n_pages):
            ot = do[i].get_text()
            at = da[i].get_text()
            o_norm = re.sub(r"\s+", " ", ot)
            a_norm = re.sub(r"\s+", " ", at)
            o_no_acc = remove_accents(o_norm.lower())
            a_no_acc = remove_accents(a_norm.lower())

            changed = (o_norm != a_norm)

            # Orphans REALES: nombre completo (apellido + algún otro token significativo
            # del nombre del LLM) coexistiendo cerca en el anonimizado.
            # Apellido suelto NO se considera orphan (por diseño).
            orphans = []
            for pname, tokens in party_tokens.items():
                surname_no_acc = remove_accents(party_surname(pname).lower())
                if len(surname_no_acc) < 4:
                    continue
                other_tokens = {t for t in tokens if t != surname_no_acc and len(t) >= 4}
                if not other_tokens:
                    continue
                # Buscar en anon: ventanas de 60 chars donde aparezca apellido + otro token
                for m in re.finditer(re.escape(surname_no_acc), a_no_acc):
                    window = a_no_acc[max(0, m.start() - 30): m.end() + 30]
                    if any(t in window for t in other_tokens):
                        orphans.append({"party": pname,
                                        "surname": surname_no_acc,
                                        "context": window})
                        break

            # Fragmentos sospechosos: placeholder seguido o precedido inmediatamente
            # de un token que parece parte del nombre (caso Rodiño suelto post placeholder).
            suspicious = []
            for pname, tokens in party_tokens.items():
                for tok in tokens:
                    # post: "----- Rodiño"
                    for m in re.finditer(r"-{2,}\s*\.?\s*" + re.escape(tok), a_no_acc):
                        suspicious.append(f"'{pname}': post-placeholder '{tok}'")
                    # pre: "Rodiño -----" (raro)
                    for m in re.finditer(re.escape(tok) + r"\s*-{2,}", a_no_acc):
                        suspicious.append(f"'{pname}': pre-placeholder '{tok}'")

            per_page.append({
                "page": i,
                "changed": changed,
                "orphans": orphans,
                "suspicious": suspicious,
            })

    return {
        "stem": stem,
        "n_pages": n_pages,
        "parties": [p["nombre"] for p in parties],
        "per_page": per_page,
    }


def interesting_pages(report: dict) -> list[int]:
    """Páginas a renderizar: las que cambiaron + las con orphans/suspicious."""
    pages = set()
    for pp in report.get("per_page", []):
        if pp["changed"] or pp["orphans"] or pp["suspicious"]:
            pages.add(pp["page"])
    return sorted(pages)


def render_pages(stem: str, pages: list[int], dpi: int = 90) -> Path:
    target = OUT / stem
    target.mkdir(parents=True, exist_ok=True)
    for kind, path in [("orig", LAB / f"{stem}.pdf"),
                       ("anon", LAB / f"{stem}_anonimizado.pdf")]:
        with fitz.open(str(path)) as d:
            for i in pages:
                p = target / f"{kind}_{i:03d}.png"
                if not p.exists():
                    pix = d[i].get_pixmap(dpi=dpi)
                    pix.save(str(p))
    return target


if __name__ == "__main__":
    for stem in sys.argv[1:]:
        r = inspect(stem)
        ip = interesting_pages(r)
        print(f"\n=== {stem} ===")
        print(f"  parties: {r['parties']}")
        print(f"  pages: {r['n_pages']} total, {len(ip)} interesantes")
        print(f"  interesting: {ip}")
        # Mostrar issues por página (orphans/suspicious)
        for pp in r["per_page"]:
            if pp["orphans"] or pp["suspicious"]:
                print(f"  p{pp['page']}: orphans={pp['orphans']} susp={pp['suspicious']}")
        if ip:
            render_pages(stem, ip)
            print(f"  rendered to {OUT / stem}")

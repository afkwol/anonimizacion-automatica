"""Smoke tests de la GUI Tkinter (PR 12).

NO lanza `mainloop()`. Sólo verifica que el módulo importa, que la
ventana se construye, y que las pestañas existen. Cualquier error de
sintaxis/import quedaría atrapado acá.
"""
from __future__ import annotations

import os

import pytest


def _has_display() -> bool:
    # En Windows tkinter siempre puede crear root sin DISPLAY.
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY"))


pytestmark = pytest.mark.skipif(not _has_display(), reason="Sin display")


def test_gui_import() -> None:
    from app.gui import app  # noqa: F401


def test_gui_construye_ventana() -> None:
    import tkinter
    try:
        from app.gui.app import AnonimizadorApp

        app = AnonimizadorApp()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk no disponible: {exc}")
    try:
        # Las 4 pestañas deben existir
        assert hasattr(app, "tab_proc")
        assert hasattr(app, "tab_review")
        assert hasattr(app, "tab_config")
        assert hasattr(app, "tab_audit")
        # Trees
        assert app.review_tree is not None
        assert app.config_tree is not None
        # Política inicial: PARTE_ACTORA anonimiza, JUEZ no
        from app.classify.taxonomy import Role
        assert app.policy.should_anonymize(Role.PARTE_ACTORA) is True
        assert app.policy.should_anonymize(Role.JUEZ) is False
    finally:
        app.destroy()


def test_config_tree_toggle() -> None:
    import tkinter
    try:
        from app.gui.app import AnonimizadorApp
        from app.classify.taxonomy import Role

        app = AnonimizadorApp()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk no disponible: {exc}")
    try:
        # Toggle JUEZ via método interno (sin click real)
        before = app.policy.should_anonymize(Role.JUEZ)
        app.policy.set(Role.JUEZ, not before)
        app._refresh_config_tree()
        assert app.policy.should_anonymize(Role.JUEZ) == (not before)
        # El tree contiene el item con iid=role.value
        assert "JUEZ" in app.config_tree.get_children()
    finally:
        app.destroy()

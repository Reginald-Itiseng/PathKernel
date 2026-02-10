from __future__ import annotations

from pathlib import Path

from app.core.project import Layer, Project


def _mk_layer(name: str) -> Layer:
    return Layer(name=name, path=Path(f"{name}.gtl"), kind="gerber", source=object())


def test_project_add_and_visible_layers() -> None:
    project = Project()
    l1 = project.add_layer(_mk_layer("top"))
    l2 = project.add_layer(_mk_layer("bottom"))

    assert len(project.layers) == 2
    assert len(project.visible_layers()) == 2

    project.set_visibility(1, False)
    assert l1.visible is True
    assert l2.visible is False
    assert project.visible_layers() == [l1]


def test_project_clear_resets_layers() -> None:
    project = Project()
    project.add_layer(_mk_layer("top"))
    project.clear()
    assert project.layers == []


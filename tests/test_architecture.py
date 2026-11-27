from scripts.check_architecture import cycle_edges, dependency_graph


def test_dependency_graph_includes_late_relative_imports_and_excludes_vendor(tmp_path):
    package = tmp_path / "homeplane"
    (package / "vendor").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "calendar.py").write_text("def event():\n    from . import compiler\n")
    (package / "compiler.py").write_text(
        "from homeplane.calendar import event\nfrom .vendor import bundled\n"
    )
    (package / "vendor" / "bundled.py").write_text("import homeplane.compiler\n")

    graph = dependency_graph(tmp_path)
    edges = cycle_edges(graph)
    assert set(graph) == {"homeplane", "homeplane.calendar", "homeplane.compiler"}
    assert edges == {
        ("homeplane.calendar", "homeplane.compiler"),
        ("homeplane.compiler", "homeplane.calendar"),
    }


def test_cycle_edges_identifies_new_edges_and_retires_removed_edges_in_same_component():
    original = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "application": {"a"}}
    revised = {"a": {"b", "c"}, "b": {"a", "c"}, "c": {"b"}, "application": {"a"}}

    before, after = cycle_edges(original), cycle_edges(revised)
    assert before - after == {("c", "a")}
    assert after - before == {("a", "c"), ("b", "a"), ("c", "b")}

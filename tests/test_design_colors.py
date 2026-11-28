import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def root_colors(stylesheet):
    root = re.search(r":root\s*\{([^}]+)\}", (ROOT / stylesheet).read_text(encoding="utf-8"))[1]
    return dict(re.findall(r"--([a-z-]+)\s*:\s*(#[\da-fA-F]+)\s*(?:;|$)", root))


def test_design_palette_matches_console_and_game_sources():
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8").split("---", 2)[1]
    colors = design.split("\ncolors:\n", 1)[1].split("\ntypography:", 1)[0]
    documented = dict(re.findall(r'^  ([a-z-]+): "(#[\da-fA-F]+)"$', colors, flags=re.MULTILINE))
    console = root_colors("homeplane/static/style.css")
    game = root_colors("homeplane/embodied/static/games.css")
    game_names = {
        "game-ink": "ink", "game-muted": "muted", "game-paper": "paper", "game-line": "line",
        "game-control-hover": "game-hover",
    }

    assert documented
    for name, color in documented.items():
        if name.startswith("game-"):
            assert game[game_names.get(name, name)] == color, name
        else:
            assert console[name] == color, name

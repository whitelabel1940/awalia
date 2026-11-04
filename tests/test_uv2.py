"""UV2 user components keep icon glyphs out of CSS and JS."""
from pathlib import Path
import re


def test_user_components_use_drawn_icons():
    static = Path(__file__).resolve().parents[1] / 'cbs/web/static'
    for path in static.rglob('*'):
        if path.suffix not in {'.css', '.js'} or path.name == 'style.css' or path.name.startswith('dev'):
            continue
        assert not set('◌▤⚙⌃⌄✓') & set(path.read_text(encoding='utf-8-sig')), path


def test_notice_and_secondary_text_contrast():
    css = (Path(__file__).resolve().parents[1] / 'cbs/web/static/screens.css').read_text(encoding='utf-8')
    def luminance(color):
        rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        return sum((v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4) * w for v, w in zip(rgb, (.2126, .7152, .0722)))
    palettes = re.findall(r'\{([^{}]*--bg:[^{}]*)\}', css)
    assert len(palettes) == 3  # light, system dark and explicit dark
    for palette in palettes:
        colors = dict(re.findall(r'--([\w-]+):(#[\da-f]{6})', palette))
        for fg, bg in [('success', 'success-soft'), *[(fg, bg) for fg in ('muted', 'faint') for bg in ('bg', 'surface', 'subtle', 'bubble')]]:
            a, b = sorted((luminance(colors[fg]), luminance(colors[bg])))
            assert (b + .05) / (a + .05) >= 4.5, (fg, bg, colors)

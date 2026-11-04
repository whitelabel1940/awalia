"""UV1: all user-facing font sizes must use the seven named tokens."""
from pathlib import Path
import re

STATIC = Path(__file__).resolve().parents[1] / 'cbs/web/static'
TOKEN = r'var\(--type-(?:page|title|section|body|ui|note|meta)\)'
SIZE = re.compile(rf'^(?:{TOKEN}|inherit)(?:\s*!important)?$')


def invalid_sizes(text):
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
    errors = []
    for match in re.finditer(r'(?<![\w-])(font-size|font)\s*:\s*([^;}\n]+)', text):
        prop, value = match.groups()
        value = value.strip()
        quoted = re.match(r"(['\"`])(.*?)\1", value)
        value = quoted[2] if quoted else value
        if prop == 'font-size':
            valid = SIZE.fullmatch(value)
        else:
            # Font shorthand may contain style/weight before the size and a
            # line-height/family afterwards, but no numeric or keyword size.
            valid = value == 'inherit' or re.fullmatch(
                rf'(?:(?:normal|italic|oblique|small-caps|bold|bolder|lighter|[1-9]00)\s+)*{TOKEN}(?:\s*/\s*(?:var\(--leading-[\w-]+\)|[\d.]+))?\s+[^;]+', value)
        if not valid:
            errors.append(match.group())
    for match in re.finditer(r"\b(fontSize|font)\s*(?:=|:)\s*(['\"`])(.+?)\2", text):
        prop = 'font-size' if match[1] == 'fontSize' else 'font'
        if invalid_sizes(prop+':'+match[3]+';'):
            errors.append(match.group())
    for match in re.finditer(r"(?:\bfontSize\s*(?:=|:)|\.font\s*=)\s*(?![\s'\"`])[^,;}\n]+", text):
        errors.append(match.group())
    for match in re.finditer(r"setProperty\(\s*['\"`](font-size|font)['\"`]\s*,\s*(['\"`])(.+?)\2", text):
        if invalid_sizes(match[1]+':'+match[3]+';'):
            errors.append(match.group())
    return errors


def test_user_font_sizes_use_type_tokens():
    errors = []
    for path in sorted(STATIC.rglob('*')):
        # R7's fixed-resolution exported PNGs use canvas pixels, not UI type sizes.
        # Keep the finished image design separate from the screen typography guard.
        if path.name == 'share-card.js':
            continue
        if path.suffix not in {'.css', '.js'} or path.name == 'style.css' or (path.suffix == '.js' and path.name.startswith('dev')):
            continue
        errors.extend(f'{path.name}: {value}' for value in invalid_sizes(path.read_text(encoding='utf-8-sig')))
    assert not errors, '\n'.join(errors)


def test_size_guard_rejects_literals_and_relative_sizes():
    for declaration in ['font-size:15px;', 'font-size:1rem;', 'font-size:80%;', 'font-size:large;', 'font:italic 16px/1.9 serif;', 'x.style.fontSize="2em"', 'const x={fontSize:12}', 'x.style.font="bold 12px serif"', 'x.style.setProperty("font-size","14px")']:
        assert invalid_sizes(declaration), declaration
    for declaration in ['font-size:var(--type-body);', 'font:inherit;', 'font:500 var(--type-page)/var(--leading-heading) var(--serif);', 'x.style.fontSize="var(--type-ui)"', "const x={font:'700 var(--type-meta)/1 system-ui',color:'red'}"]:
        assert not invalid_sizes(declaration), declaration


def test_type_tokens_match_design():
    css = (STATIC / 'screens.css').read_text(encoding='utf-8')
    for tier, size in {'page':'30','title':'23','section':'19','body':'16','ui':'14.5','note':'13.5','meta':'12'}.items():
        assert f'--type-{tier}:{size}px' in css


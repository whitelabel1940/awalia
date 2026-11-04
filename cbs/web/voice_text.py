"""Shared default for settings and bootstrap voice previews."""
import json
from pathlib import Path

VOICE_PREVIEW_TEXT = json.loads((Path(__file__).parent / 'static' / 'voice-preview.json').read_text(encoding='utf-8'))['text']

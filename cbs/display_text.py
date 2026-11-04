"""Unicode script checks only; no language or meaning classification."""
import regex


# Common includes punctuation, numbers, symbols and emoji; Inherited includes
# combining accents/variation selectors. Script (not block/name) handles extensions.
_UNEXPECTED = regex.compile(r'[^\p{sc=Latin}\p{sc=Han}\p{sc=Hiragana}\p{sc=Katakana}\p{sc=Common}\p{sc=Inherited}]')


def script_mix(text):
    return bool(_UNEXPECTED.search(text or ''))


def display_call(generate, texts, *, validate=None):
    """Repeat once on script mix or failed validation; never return bad text.

    The caller selects only generated display fields, excluding IDs, URLs, source
    quotes and original records. Callers keep their existing persistence boundary.
    """
    for attempt in range(2):
        result = generate()
        if (not any(script_mix(t) for t in texts(result) if t is not None)
                and (validate is None or validate(result))):
            return result
    raise ValueError('display text rejected after one retry')

"""Remove exact copies only; mutable state always stays in the turn payload."""
import json

from cbs.runtime import read_prompt, character_prefix


def compact_context(context, static):
    result = dict(context)
    if context.get('character_summary') == static.get('character_summary'):
        result.pop('character_summary')
    catalog = {d['id']: d for d in static.get('domain_catalog', [])}
    result['domains'] = [{k:v for k,v in d.items() if k == 'id' or catalog.get(d['id'], {}).get(k) != v}
                         for d in context.get('domains', [])]
    # Same plan records already occur in candidates; active plans are still
    # passed in full to Packet and Check, independently of candidate selection.
    result.pop('unfinished_ledger', None)
    return result


def prejudge_input(character, context):
    prefix = character_prefix(character, catalog=True)
    # character_prefix is our own JSON suffix, never arbitrary model output.
    static = json.loads(prefix[prefix.index('{"static_character":'):])['static_character']
    return read_prompt('pre_judge') + prefix, compact_context(context, static)

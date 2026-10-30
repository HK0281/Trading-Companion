"""Bounded recovery of misplaced numeric bindings and visible citation markers."""
import json
import re

_ARRAY = re.compile(r'\[\s*\{')
_REF = re.compile(r'(?<![A-Za-z0-9_.])(?:E|L)\d+(?![A-Za-z0-9_.])')
_EMPTY = re.compile(r'[（(]\s*(?:[/、,，;；]\s*)*[）)]')


def prepare_claim(text, numbers, context, instrument):
    from trading_companion.answer_numeric_bindings import InvalidBinding, resolve_ref
    from trading_companion.answer_validation import HARD, unsafe
    from trading_companion.product_answer import _unique, _constant
    if type(numbers) is not list or len(numbers) > 80:
        raise InvalidBinding('invalid_numbers_array')
    decoder = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_constant)
    parts, offset, recovered, bindings = [], 0, 0, list(numbers)
    for _ in range(81):
        match = _ARRAY.search(text, offset)
        if match is None:
            break
        if unsafe(text):
            raise InvalidBinding('unsafe_inline_binding', HARD)
        try:
            value, end = decoder.raw_decode(text, match.start())
        except (ValueError, RecursionError):
            raise InvalidBinding('invalid_inline_binding') from None
        if type(value) is not list or not value or len(bindings) + len(value) > 80:
            raise InvalidBinding('invalid_inline_binding')
        for item in value:
            if (type(item) is not dict or type(item.get('text')) is not str or not item['text']
                    or _ARRAY.search(item['text'])):
                raise InvalidBinding('invalid_inline_binding')
            keys = set(item)
            if not (keys in ({'text', 'ref'}, {'text', 'ref', 'field'})
                    or keys == {'text', 'derive', 'refs'}):
                raise InvalidBinding('invalid_inline_binding_fields')
        parts.extend((text[offset:match.start()], ''.join(item['text'] for item in value)))
        bindings.extend(value)
        offset, recovered = end, recovered + 1
    else:
        raise InvalidBinding('too_many_inline_bindings')
    text = ''.join(parts) + text[offset:]
    if len(text) > 1800:
        raise InvalidBinding('recovered_claim_too_long')
    used = []
    def clear(match):
        _, ids = resolve_ref(match[0], context, instrument)
        used.extend(ids)
        return ' ' * len(match[0])
    # Preserve positions and sentence boundaries, including reference-only sentences.
    text = _REF.sub(clear, text)
    text = _EMPTY.sub(lambda m: ' ' * len(m[0]), text)
    return text, bindings, used, recovered

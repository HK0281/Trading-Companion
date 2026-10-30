"""Bounded JSON framing recovery; decoded values are never rewritten."""
from dataclasses import dataclass
import json

from trading_companion.product_answer import _unique, _constant
from trading_companion.answer_validation import HARD, STRUCTURE, unsafe


@dataclass(frozen=True)
class ParsedNarrative:
    payload: object
    raw_invalid: bool = False
    recovered: bool = False
    partial: bool = False
    punctuation_repaired: bool = False


def _space(text, offset):
    while offset < len(text) and text[offset].isspace():
        offset += 1
    return offset


def _prefix_object(text, start, decoder):
    """Keep only complete fields/items in a malformed object's unambiguous prefix."""
    result, seen, offset = {}, set(), start+1
    while True:
        offset = _space(text, offset)
        try:
            key, offset = decoder.raw_decode(text, offset)
        except json.JSONDecodeError:
            break
        if type(key) is not str or key in seen:
            raise ValueError(STRUCTURE)
        seen.add(key)
        offset = _space(text, offset)
        if offset >= len(text) or text[offset] != ':':
            break
        offset = _space(text, offset+1)
        try:
            value, following = decoder.raw_decode(text, offset)
        except json.JSONDecodeError:
            if key not in ('narratives', 'decisions') or text[offset:offset+1] != '[':
                break
            items, cursor = [], offset+1
            while len(items) < 3:
                try:
                    item, cursor = decoder.raw_decode(text, _space(text, cursor))
                except json.JSONDecodeError:
                    break
                if type(item) is not dict:
                    break
                items.append(item)
                cursor = _space(text, cursor)
                if cursor >= len(text) or text[cursor] != ',':
                    break
                cursor += 1
            if items:
                result[key] = items
            break
        result[key], offset = value, _space(text, following)
        if offset >= len(text) or text[offset] != ',':
            break
        offset += 1
    if result.get('version') != 'product-research-answer-v3' or not any(
            type(result.get(k)) is list and result[k] for k in ('narratives','decisions')):
        raise ValueError(STRUCTURE)
    return result


def _repair_punctuation(text, start, decoder):
    """At most four punctuation edits; strings, keys and values are never repaired."""
    stack, output, edits = [], [], 0
    quoted = escaped = False
    end = len(text)
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '{[':
            stack.append('}' if char == '{' else ']')
            if len(stack) > 64:
                return None
        elif char == ',':
            following = _space(text, index+1)
            if following == len(text) or text[following] in '}]':
                edits += 1
                if edits > 4:
                    return None
                continue
        elif char in '}]':
            if char not in stack:
                return None
            while stack[-1] != char:
                output.append(stack.pop())
                edits += 1
                if edits > 4:
                    return None
            stack.pop()
            if not stack:
                output.append(char)
                end = index+1
                break
        output.append(char)
    if quoted or edits+len(stack) > 4:
        return None
    edits += len(stack)
    output.extend(reversed(stack))
    if not edits:
        return None
    try:
        payload = decoder.decode(''.join(output))
    except json.JSONDecodeError:
        return None
    return payload, end


def _check_suffix(suffix):
    if any(character in suffix for character in '{}[]') or suffix.count('```') > 1:
        raise ValueError(STRUCTURE)


def parse_narrative(text):
    if type(text) is not str or len(text) > 26000:
        raise ValueError(STRUCTURE)
    decoder = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_constant)
    try:
        return ParsedNarrative(decoder.decode(text))
    except json.JSONDecodeError:
        pass
    # Do not discard an unsafe wrapper to turn a hard rejection into a success.
    if unsafe(text):
        raise ValueError(HARD)
    start = text.find('{')
    if start < 0 or any(character in text[:start] for character in '{}[]') or text.count('```') > 2:
        raise ValueError(STRUCTURE)
    try:
        payload, end = decoder.raw_decode(text, start)
    except json.JSONDecodeError as error:
        repaired = _repair_punctuation(text, start, decoder)
        if repaired is not None:
            payload, end = repaired
            _check_suffix(text[end:])
            return ParsedNarrative(payload, True, True, False, True)
        # An invalid token in the middle can conceal another object or a duplicate
        # field. Only an actual incomplete suffix is eligible for prefix recovery.
        if error.pos < len(text.rstrip()) and not error.msg.startswith('Unterminated string'):
            raise ValueError(STRUCTURE) from error
        return ParsedNarrative(_prefix_object(text, start, decoder), True, True, True)
    suffix = text[end:]
    # A second object/array or another code block is ambiguous, not a repair.
    _check_suffix(suffix)
    return ParsedNarrative(payload, True, True, False)

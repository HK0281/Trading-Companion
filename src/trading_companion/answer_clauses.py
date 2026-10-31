"""Position-preserving clause boundaries shared by validation and local recovery."""
import re


def has_empty_slot(text, removed_spans=(), *, subject_required=False):
    """Recognize definite missing operands, without judging general Chinese prose."""
    # Citation cleanup preserves spaces. Only a clause-initial preposition plus
    # a missing object/copula/unit or a bare preposition is unambiguous here.
    boundary = r'(?:^|[，,；;。！？!?\n])\s*'
    if subject_required and re.match(r'^\s*(?:处于|位于|处在|均处|均位于|仅作|只作|仅为|显示|表明|说明|意味着)', text):
        return True
    if (re.search(boundary + r'(?:自|至|从|到|在|于|为)(?:'
        r'\s*(?=[，,；;。！？!?]|$)|[ \t]+(?=为|是|元(?:/股)?))', text)
        or re.search(boundary + r'自[ \t]+(?:向上突破|向下跌破|升破|跌破|站稳)', text)):
        return True
    # Inside a clause, require a known removed reference, rather than guessing
    # whether whitespace or the last character of a normal word means a hole.
    for start, end in removed_spans:
        before = text[:start].replace('**', '').rstrip()
        after = text[end:].replace('**', '').lstrip()
        if (re.search(r'(?:自|至|从|到|在|于|以)$|'
                      r'(?:价位|价格|收盘价?|入场|止损|目标|区间|均线|读数|数值)(?:为|在|于)$', before)
                and re.match(r'(?:$|[，,；;。！？!?]|为|是|元(?:/股)?|附近|一带|以上|以下|向上突破|向下跌破)', after)):
            return True
    return False


def clause_spans(text):
    start, stack, output = 0, [], []
    pairs = {'(': ')', '（': '）', '[': ']', '【': '】', '“': '”', '「': '」'}
    for index, char in enumerate(text):
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in '，,；;。！？!?\n':
            if char == ',' and index and text[index - 1].isdigit() and re.match(r'\d{3}(?:\D|$)', text[index + 1:]):
                continue
            if text[start:index].strip():
                output.append((start, index, text[start:index]))
            start = index + 1
    if text[start:].strip():
        output.append((start, len(text), text[start:]))
    return output


def independent_remainder(original, clauses):
    """Never turn a dependent condition, negation or causal argument into a fact."""
    if re.search(r'如果|假如|一旦|除非|只有|只要|否则|若|则|才会|因为|因此|所以|虽然|但|然而|不过|反之|并非|不是|而是|并不|不代表|不能|未必', original):
        return None
    cleaned = [part.strip(' \t，,；;。！？!?') for part in clauses]
    if any(not part or re.fullmatch(r'(?:但|且|而|并|或|及|同时|以及|不过|然而|并且|而且|但是)', part)
           or has_empty_slot(part)
           or re.fullmatch(r'(?:约|为|是|读数为?)?\s*[-+]?[\d.,]+\s*(?:亿元|万元|元/股|元|%|倍|点)?', part)
           or len(re.sub(r'[^\u4e00-\u9fffA-Za-z]', '', part)) < 2 for part in cleaned):
        return None
    if not cleaned:
        return None
    return '，'.join(cleaned) + '。'

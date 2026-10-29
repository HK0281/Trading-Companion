"""Literal directory identities in research questions; predicate words are not extra stocks."""
import re

from trading_companion.research_decision import wants_decision
from trading_companion.security_directory import _normalized


def research_spans(question, directory):
    spans = directory.mentioned_spans(question)
    output = []
    for start, end in spans:
        mention = question[start:end]
        earlier = [span for span in output if span[1] <= start
                   and directory.lookup(question[span[0]:span[1]]).status == 'resolved']
        # A terminal evaluative predicate after an identified subject is grammar,
        # while a quoted/name-introduced/list member stays a literal identity.
        tail = question[end:].strip()
        prefix = question[:start].rstrip()
        named = re.search(r'(?:和|与|及|、|比|对比|比较|对照|股票|公司|证券|名为|叫|[“"「『])\s*$', prefix)
        predicate = (earlier and not named and wants_decision(mention+'吗')
                     and re.fullmatch(r'[吗么呢?？。!！\s]*', tail) is not None)
        if not predicate:
            output.append((start, end))
    return tuple(output)


def grounded_mention(mention, question, directory):
    if mention in question:
        return True
    # Permit representation differences, never a remembered name-to-code mapping.
    return any(_normalized(mention) == _normalized(question[a:b])
               for a,b in research_spans(question, directory))


def complete_mentions(question, mentions, directory):
    spans = research_spans(question, directory)
    all_spans = directory.mentioned_spans(question)
    supplied = [question[a:b] for a,b in spans if directory.lookup(question[a:b]).status == 'resolved']
    for mention in mentions:
        occurrences = [(m.start(),m.end()) for m in re.finditer(re.escape(mention),question)]
        normalized = [question[a:b] for a,b in spans if _normalized(mention)==_normalized(question[a:b])]
        if normalized:
            supplied.extend(normalized)
        elif occurrences and all(any(a<=s and e<=b for a,b in all_spans) for s,e in occurrences):
            # Covered by a complete literal identity, or a discarded predicate.
            if not all(any(a<=s and e<=b and directory.lookup(question[a:b]).status=='resolved'
                           for a,b in spans) or any(a<=s and e<=b and (a,b) not in spans
                                                    for a,b in all_spans) for s,e in occurrences):
                supplied.append(mention)
        else:
            supplied.append(mention)
    return tuple(dict.fromkeys(supplied))

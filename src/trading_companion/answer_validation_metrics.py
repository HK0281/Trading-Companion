"""Version-aware clause statistics; missing historical counters stay unknown."""
from trading_companion.answer_numeric_bindings import CLAUSE_COUNTERS, RELAXATION_COUNTERS


def clause_metrics(qualities):
    qualities = list(qualities)
    known = [q for q in qualities if q.get('sentence_count_version') == 'sentence-counts-v2'
             and all(type(q.get(key)) is int and q[key] >= 0 for key in CLAUSE_COUNTERS)]
    unknown = len(qualities) - len(known)
    totals = {key: sum(q[key] for q in known) for key in CLAUSE_COUNTERS}
    result = {key: value if not unknown else None for key, value in totals.items()}
    result.update(sentence_count_version='sentence-counts-v2' if known and not unknown else None,
                  clause_counts_unknown=unknown, clause_known_subtotals=totals,
                  clause_deletion_rate=(totals['deleted_clauses'] / totals['generated_clauses']
                                        if not unknown and totals['generated_clauses'] else None))
    result['validation_relaxations'] = {
        key: sum(q.get(key, 0) for q in known) if not unknown else None for key in RELAXATION_COUNTERS}
    return result


def deletion_metrics(qualities):
    """Comparable v2 sentence and clause rates for one explicit turn scope."""
    qualities = list(qualities)
    known = [q for q in qualities if q.get('sentence_count_version') == 'sentence-counts-v2'
             and all(type(q.get(k)) is int and q[k] >= 0 for k in ('generated_sentences', 'deleted_sentences'))]
    generated = sum(q['generated_sentences'] for q in known)
    deleted = sum(q['deleted_sentences'] for q in known)
    unknown = len(qualities) - len(known)
    return dict(**clause_metrics(qualities), generated_sentences=generated if not unknown else None,
                deleted_sentences=deleted if not unknown else None, sentence_counts_unknown=unknown,
                known_generated_sentences=generated, known_deleted_sentences=deleted,
                sentence_deletion_rate=deleted/generated if not unknown and generated else None)

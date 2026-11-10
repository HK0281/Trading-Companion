"""Assert the additive diagnostics contract before comparing immutable old captures."""

from copy import deepcopy

from tests.routing_baseline_support import compare_observations as compare_historical


def compare_observations(scenario, observations, *, diagnostic_dir=None):
    compatible = deepcopy(observations)
    for observation in compatible:
        result = observation['outcome'].get('response', {}).get('result', {})
        if result.get('code') == 'general_knowledge_answered':
            knowledge = result.get('knowledge')
            if not isinstance(knowledge, dict) or knowledge.get('diagnostics') != {
                    'version': 'knowledge-validation-v2', 'state': 'accepted', 'reason': None,
                    'first_reason': None, 'rewrite_attempts': 0, 'acceptance': 'first_pass', 'adjustments': []}:
                raise AssertionError('Successful knowledge answer must expose exactly the fixed accepted diagnostics')
            # Remove only this asserted additive field. Every old field, state and call still matches the frozen digest.
            del knowledge['diagnostics']
    return compare_historical(scenario, compatible, diagnostic_dir=diagnostic_dir)

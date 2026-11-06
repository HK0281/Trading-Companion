"""Read pinned Git blobs in a fresh interpreter; never alter the checkout."""

from importlib.abc import Loader, MetaPathFinder
from importlib.util import spec_from_loader
from pathlib import Path
import json
import subprocess
import sys
from unittest.mock import patch
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
REVISION = 'a11323834b6d0b0d615b39d896bb34c83ba905e4'


def fixed_index_observe(scenario, **options):
    from tests.intent_integration_support import observe
    with patch('trading_companion.index_contracts.uuid4', return_value=UUID(int=1)), patch(
            'trading_companion.index_agent.uuid4', return_value=UUID(int=1)):
        return observe(scenario, **options)


class HistoricalSource(MetaPathFinder, Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'trading_companion':
            name, package = 'src/trading_companion/__init__.py', True
        elif fullname.startswith('trading_companion.'):
            name, package = 'src/' + fullname.replace('.', '/') + '.py', False
        else:
            return None
        try:
            source = subprocess.check_output(['git', 'show', REVISION + ':' + name], cwd=ROOT,
                                             stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            raise ImportError('Source absent from historical snapshot: ' + fullname) from None
        spec = spec_from_loader(fullname, self, origin=str(ROOT/name), is_package=package)
        spec.loader_state = source
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = module.__spec__.origin
        exec(compile(module.__spec__.loader_state, module.__file__, 'exec'), module.__dict__)


def historical_index_observation(scenario):
    require_historical_revision()
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve())],
        input=json.dumps(scenario, ensure_ascii=True), cwd=ROOT, text=True, encoding='utf-8',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return json.loads(result.stdout)


def require_historical_revision():
    result = subprocess.run(['git', 'cat-file', '-e', REVISION+'^{commit}'], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise AssertionError('Required historical commit '+REVISION+
            ' is unavailable; restore local Git history before running the baseline comparison. '
            'This check cannot silently pass or substitute current source.')


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    sys.meta_path.insert(0, HistoricalSource())
    from tests.routing_baseline_support import project_observations
    print(json.dumps(project_observations(fixed_index_observe(json.load(sys.stdin))), ensure_ascii=True))


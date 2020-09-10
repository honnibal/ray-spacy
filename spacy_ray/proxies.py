from typing import Dict, Set, Iterable, Any, Optional
from collections import Counter
from thinc.types import FloatsXd
from .util import make_key, KeyT


class RayPeerProxy:
    """Proxy for workers where each worker owns some of the parameters. For
    parameters they don't own, workers will pull parameters and push gradients.
    For parameters they do own, they pull gradients, make the update, and push
    parameters.
    """

    ray: Any
    _params: Dict[KeyT, FloatsXd]
    _versions: Dict[KeyT, int]
    _owned_keys: Set[KeyT]
    _grads: Dict

    def __init__(
        self,
        peers: Dict[KeyT],
        optimizer,
        keys: Iterable[KeyT],
        *,
        grads_per_update: int = 2,
        ray=None
    ):
        if ray is None:
            import ray  # type: ignore
        # Pass in 'ray' so that we can test with a mock object.
        self.ray = ray
        self.optimizer = optimizer
        self.grads_per_update = grads_per_update
        self.peers = dict(peers)
        self._owned_keys = set(keys)
        self.other_workers = set()
        for key, peer in self.peers.items():
            if key not in self._owned_keys and peer not in self.other_workers:
                self.other_workers.add(peer)
        self._params = {}
        self._versions = Counter()
        self._grads = {}
        self._grad_counts = Counter()
        self._futures = []
    
    def check_version(self, key: KeyT, version: int) -> Optional[bool]:
        if key not in self._versions:
            return None
        elif self._versions[key] != version:
            return False
        else:
            return True

    def set_param(self, id, name, value: FloatsXd) -> None:
        """Set a parameter to the connection."""
        key = make_key(id, name)
        if key in self._owned_keys or key not in self._params:
            self._params[key] = value
            self._versions[key] += 1
            self._grads[key] = None
            self._grad_counts[key] = 0

    def send_param(self, key):
        param = self._params[key]
        version = self._versions[key]
        for peer in self.other_workers:
            peer.set_param.remote(key, version, param)

    def receive_param(self, key, version, value: FloatsXd) -> None:
        """Let the connection push a parameter to us."""
        self._params[key] = value
        self._versions[key] = version
        self._grads[key] = None
        self._grad_counts[key] = 0

    def get_param(self, id, name) -> FloatsXd:
        key = make_key(id, name)
        self._maybe_update_param(key)
        return self._params[key]

    def set_grad(self, id, name, value: FloatsXd) -> None:
        """Set a gradient to the connection."""
        key = make_key(id, name)
        if key in self._owned_keys:
            self._grads[key] = value
            self._grad_counts[key] = 1

    def inc_grad(self, id, name, value: FloatsXd) -> None:
        """Increment a gradient to the connection."""
        key = make_key(id, name)
        self._grad_counts[key] += 1
        if key not in self._owned_keys:
            peer = self.peers[key]
            peer.inc_grad.remote(key, self._versions[key], value)
        else:
            if self._grads.get(key) is None:
                self._grads[key] = value.copy()
            else:
                self._grads[key] += value

    def _maybe_update_param(self, key):
        if key not in self._owned_keys:
            return False
        elif self._grad_counts[key] < self.grads_per_update:
            return False
        else:
            self._versions[key] += 1
            param, _ = self.optimizer(key, self._params[key], self._grads[key])
            self._params[key] = param
            self._grads[key] = None
            self._grad_counts[key] = 0
            self.send_param(key)
            return True
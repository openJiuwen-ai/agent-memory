"""Bound graph queries and reuse verified reads within a request."""

from dataclasses import asdict

from jiuwen_memory.common.type_def import RecallChannel, RecallResult
from jiuwen_memory.retrieval.expander import node_key


class _RequestReads:
    """Cache only within one authorized search; bound distinct reads across stages."""

    def __init__(self, domain, policy, candidates, *, recall_channels=None):
        self.domain, self.policy = domain, policy
        self._recall_channels = None if recall_channels is None else frozenset(recall_channels)
        self.units = {node_key(c.unit): c.unit for c in candidates}
        self.attempted = set(self.units)
        self.reads, self.edge_ids = 0, set()
        self.edge_cache = {}
        self.recall_cache, self.vector_scopes = {}, set()
        self.content_matches = {}
        if getattr(policy, "chunk_source_hints", False):
            from .graphmem_chunk_hints import valid_match

            self.content_matches = {node_key(c.unit): c.content_match for c in candidates
                                    if valid_match(c.content_match)}
        self.limited = False
        self.read_limited = False
        self.edge_limited = False

    @staticmethod
    def key(scope, uid):
        return (scope.org, scope.space, scope.user, scope.agent, scope.session, uid)

    def get(self, scope, ids):
        wanted = [uid for uid in dict.fromkeys(ids) if self.key(scope, uid) not in self.attempted]
        room = max(0, self.policy.max_reads - self.reads)
        self.limited |= len(wanted) > room
        self.read_limited |= len(wanted) > room
        wanted = wanted[:room]
        if wanted:
            self.reads += len(wanted)
            self.attempted.update(self.key(scope, uid) for uid in wanted)
            # Authentication/plaintext/backend exceptions deliberately propagate.
            for unit in self.domain.get(scope, wanted):
                if unit.scope == scope and unit.id in wanted:
                    self.units[node_key(unit)] = unit
        return [
            self.units[self.key(scope, uid)] for uid in ids if self.key(scope, uid) in self.units
        ]

    def related_edges(self, scope, ids, *, relation, limit):
        if self._recall_channels is not None and RecallChannel.GRAPH not in self._recall_channels:
            return []
        key = (self.key(scope, ""), tuple(sorted(set(ids))), relation, limit)
        if key not in self.edge_cache:
            room = max(0, self.policy.max_edges - len(self.edge_ids))
            if not room:
                self.limited = True
                self.edge_limited = True
                return []
            edges = self.domain.related_edges(
                scope, ids, relation=relation, limit=min(limit, room + 1)
            )
            retained = []
            for edge in edges:
                identity = self.key(scope, edge.id)
                if identity not in self.edge_ids and len(self.edge_ids) >= self.policy.max_edges:
                    self.limited = True
                    self.edge_limited = True
                    continue
                self.edge_ids.add(identity)
                retained.append(edge)
            self.edge_cache[key] = retained
        return self.edge_cache[key]

    def recall(self, scope, query, *, channels, recall_limit):
        if self._recall_channels is not None:
            channels = [channel for channel in channels if channel in self._recall_channels]
            if not channels:
                return RecallResult()
        # Navigation only requests IDs/scores from already reached source scopes.
        # Context stages share this cap and identical-query result cache; final
        # source materialization still goes through get's separate read budget.
        skey = self.key(scope, "")
        key = (skey, repr(asdict(query)), tuple(channels), recall_limit)
        if key not in self.recall_cache:
            if (
                skey not in self.vector_scopes
                and len(self.vector_scopes) >= self.policy.source_vector_scopes
            ):
                self.limited = True
                return RecallResult()
            self.vector_scopes.add(skey)
            self.recall_cache[key] = self.domain.recall(
                scope, query, channels=channels, recall_limit=recall_limit
            )
            if getattr(self.policy, "chunk_source_hints", False):
                from .graphmem_chunk_hints import collect_content_matches, match_order

                for uid, match in collect_content_matches(self.recall_cache[key].batches).items():
                    identity = self.key(scope, uid)
                    old = self.content_matches.get(identity)
                    if old is None or match_order(match) < match_order(old):
                        self.content_matches[identity] = match
        return self.recall_cache[key]

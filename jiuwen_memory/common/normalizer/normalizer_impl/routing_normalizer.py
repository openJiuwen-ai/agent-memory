"""Route raw payloads to a modality-specific normalizer."""

from __future__ import annotations

from collections.abc import Mapping

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.normalizer.base import Normalizer, NormalizerProducer
from jiuwen_memory.common.type_def import Modality, RawPayload


class RoutingNormalizer(Normalizer):
    """Delegate normalization while keeping one Ingestor entry point."""

    def __init__(
        self,
        fallback: Normalizer,
        routes: Mapping[Modality, Normalizer],
    ) -> None:
        self._fallback = fallback
        self._routes = dict(routes)

    def modalities(self) -> list[Modality]:
        supported = set(self._fallback.modalities())
        supported.update(self._routes)
        return sorted(supported, key=lambda item: item.value)

    def plugin_type(self) -> PluginType:
        return PluginType.NORMALIZER

    def health(self) -> None:
        seen: set[int] = set()
        for normalizer in [self._fallback, *self._routes.values()]:
            if id(normalizer) in seen:
                continue
            seen.add(id(normalizer))
            normalizer.health()

    def normalize(self, payload: RawPayload) -> str:
        normalizer = self._routes.get(payload.modality, self._fallback)
        if payload.modality not in normalizer.modalities():
            raise ValidationError(
                f"no normalizer configured for modality {payload.modality.value!r}"
            )
        return normalizer.normalize(payload)


def _build_normalizer(config, value: object, *, field: str) -> Normalizer:
    if isinstance(value, str):
        return NormalizerProducer.build_named(value, config.ctx)
    if isinstance(value, Mapping):
        target = str(value.get("target", "")).strip()
        if not target:
            raise ValidationError(f"routing normalizer {field!r} is missing target")
        return NormalizerProducer.build(
            target,
            value.get("params", {}),
            config.ctx,
            name=str(value.get("name", "")),
        )
    raise ValidationError(
        f"routing normalizer {field!r} must be a named reference or component mapping"
    )


@NormalizerProducer.register("routing")
def _build(config):
    fallback_raw = config.get("fallback", {"target": "passthrough"})
    fallback = _build_normalizer(config, fallback_raw, field="fallback")
    routes_raw = config.get("routes", {})
    if not isinstance(routes_raw, Mapping):
        raise ValidationError("routing normalizer routes must be a mapping")
    routes: dict[Modality, Normalizer] = {}
    for modality_name, raw in routes_raw.items():
        try:
            modality = Modality(str(modality_name))
        except ValueError as exc:
            raise ValidationError(
                f"unknown routing normalizer modality {modality_name!r}"
            ) from exc
        routes[modality] = _build_normalizer(
            config,
            raw,
            field=f"routes.{modality.value}",
        )
    return RoutingNormalizer(fallback, routes)

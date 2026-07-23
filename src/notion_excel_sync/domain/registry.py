from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator

from notion_excel_sync.domain.analyzer import DomainAnalyzer
from notion_excel_sync.models import AnalyzerLifecycle


class AnalyzerRegistryError(ValueError):
    pass


class AnalyzerRegistry:
    """Version-aware collection of installed analyzers and their dependencies."""

    def __init__(self, analyzers: Iterable[DomainAnalyzer] = ()) -> None:
        self._analyzers: dict[str, DomainAnalyzer] = {}
        for analyzer in analyzers:
            self.register(analyzer)

    def register(self, analyzer: DomainAnalyzer, *, replace: bool = False) -> None:
        name = analyzer.manifest.name
        if name in self._analyzers and not replace:
            raise AnalyzerRegistryError(f"analyzer {name!r} is already registered")
        self._analyzers[name] = analyzer
        self._validate_dependencies_exist(allow_missing=True)

    def unregister(self, name: str) -> DomainAnalyzer:
        try:
            return self._analyzers.pop(name)
        except KeyError as exc:
            raise AnalyzerRegistryError(f"unknown analyzer {name!r}") from exc

    def get(self, name: str) -> DomainAnalyzer:
        try:
            return self._analyzers[name]
        except KeyError as exc:
            raise AnalyzerRegistryError(f"unknown analyzer {name!r}") from exc

    def __contains__(self, name: object) -> bool:
        return name in self._analyzers

    def __iter__(self) -> Iterator[DomainAnalyzer]:
        return iter(self._analyzers.values())

    def active(self, *, include_shadow: bool = False) -> list[DomainAnalyzer]:
        states = {AnalyzerLifecycle.ACTIVE, AnalyzerLifecycle.VERIFIED}
        if include_shadow:
            states.add(AnalyzerLifecycle.SHADOW)
        return [analyzer for analyzer in self if analyzer.manifest.lifecycle in states]

    def dependency_closure(self, names: Iterable[str]) -> set[str]:
        selected = set(names)
        pending = list(selected)
        while pending:
            name = pending.pop()
            analyzer = self.get(name)
            for dependency in analyzer.manifest.dependencies:
                if dependency not in self._analyzers:
                    raise AnalyzerRegistryError(
                        f"{name!r} depends on unregistered analyzer {dependency!r}"
                    )
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
        return selected

    def ordered(self, names: Iterable[str]) -> list[DomainAnalyzer]:
        """Return dependency-first order and reject dependency cycles."""

        selected = self.dependency_closure(names)
        incoming: dict[str, int] = {name: 0 for name in selected}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for name in selected:
            for dependency in self.get(name).manifest.dependencies:
                if dependency in selected:
                    incoming[name] += 1
                    outgoing[dependency].append(name)

        def sort_key(name: str) -> tuple[int, str]:
            manifest = self.get(name).manifest
            return manifest.priority, manifest.name

        ready = sorted((name for name, degree in incoming.items() if degree == 0), key=sort_key)
        order: list[str] = []
        while ready:
            name = ready.pop(0)
            order.append(name)
            for dependent in outgoing[name]:
                incoming[dependent] -= 1
                if incoming[dependent] == 0:
                    ready.append(dependent)
                    ready.sort(key=sort_key)
        if len(order) != len(selected):
            cyclic = sorted(name for name, degree in incoming.items() if degree > 0)
            raise AnalyzerRegistryError(f"analyzer dependency cycle: {', '.join(cyclic)}")
        return [self.get(name) for name in order]

    def validate(self) -> None:
        self._validate_dependencies_exist(allow_missing=False)
        self.ordered(self._analyzers)

    def _validate_dependencies_exist(self, *, allow_missing: bool) -> None:
        if allow_missing:
            return
        for analyzer in self:
            for dependency in analyzer.manifest.dependencies:
                if dependency not in self._analyzers:
                    raise AnalyzerRegistryError(
                        f"{analyzer.manifest.name!r} depends on unregistered {dependency!r}"
                    )


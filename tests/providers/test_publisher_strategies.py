"""Declared publisher DOI prefixes must route to their matching strategies."""

from scansci_pdf.publisher_strategies import StrategyRegistry


def test_registry_routes_every_declared_prefix():
    for strategy in StrategyRegistry.list_all():
        for prefix in strategy.doi_prefixes:
            assert StrategyRegistry.get_for_doi(f"{prefix}test.0001") is strategy

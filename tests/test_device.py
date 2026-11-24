"""Device naming."""

from __future__ import annotations

from custom_components.open_parcels.device import connection_device_name

CARRIERS = {"dhl": "DHL", "gls": "GLS"}


def test_connection_device_name() -> None:
    """Carrier plus label, without repeating the carrier."""
    assert connection_device_name(
        {"provider": "dhl", "label": "Germany (x)"}, CARRIERS
    ) == ("DHL Germany (x)")
    assert connection_device_name(
        {"provider": "gls", "label": "gls home"}, CARRIERS
    ) == ("gls home")
    assert connection_device_name({"provider": "dhl", "label": ""}, CARRIERS) == "DHL"
    assert connection_device_name({"provider": "", "label": "Mail"}, CARRIERS) == "Mail"

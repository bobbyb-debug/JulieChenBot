from datetime import timedelta

from services.logger import generate_session_id, shutdown_banner, startup_banner


def test_session_id_format():
    session_id = generate_session_id()

    assert isinstance(session_id, str)
    assert len(session_id) == 8
    int(session_id, 16)  # raises if not valid hex


def test_startup_banner_contains_required_fields():
    banner = startup_banner("4db20a87")

    assert "Julie ChenBot" in banner
    assert "Version :" in banner
    assert "Phase   :" in banner
    assert "Build   :" in banner
    assert "Python  :" in banner
    assert "Started :" in banner
    assert "UTC" in banner
    assert "Session : 4db20a87" in banner
    assert "Expect the unexpected." in banner


def test_shutdown_banner_contains_required_fields_when_values_supplied():
    banner = shutdown_banner(
        session_id="4db20a87",
        uptime=timedelta(hours=1, minutes=2, seconds=3),
        tick_count=42,
        error_count=0,
    )

    assert "Uptime      : 1:02:03" in banner
    assert "Tick Count  : 42" in banner
    assert "Error Count : 0" in banner
    assert "Session     : 4db20a87" in banner
    assert "Goodbye. And remember to love one another." in banner


def test_shutdown_banner_degrades_gracefully_when_values_omitted():
    """
    shutdown_banner() itself no longer knows anything about
    ProductionEngine/Scheduler — it just needs to handle a
    caller passing None for any of the runtime values.
    """

    banner = shutdown_banner(session_id="4db20a87")

    assert "Uptime      : N/A" in banner
    assert "Tick Count  : N/A" in banner
    assert "Error Count : N/A" in banner
    assert "Goodbye. And remember to love one another." in banner
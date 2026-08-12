from platforms.chatgpt.browser_engine import CAMOUFOX_WINDOW_SIZES, CamoufoxEngine


def test_attempt_profile_controls_stable_window_dimensions():
    profile = {"viewport": {"width": 1440, "height": 900}}
    headed = CamoufoxEngine(
        headless=False,
        profile=profile,
        attempt_id="same-attempt",
        system_name="Windows",
    )
    headless = CamoufoxEngine(
        headless=True,
        profile=profile,
        attempt_id="same-attempt",
        system_name="Windows",
    )

    assert headed.build_launch_options()["window"] == (1440, 900)
    assert headless.build_launch_options()["window"] == (1440, 900)


def test_attempt_without_profile_gets_supported_deterministic_window():
    first = CamoufoxEngine(
        headless=False,
        attempt_id="deterministic-attempt",
        system_name="Windows",
    ).build_launch_options()["window"]
    second = CamoufoxEngine(
        headless=False,
        attempt_id="deterministic-attempt",
        system_name="Windows",
    ).build_launch_options()["window"]

    assert first == second
    assert first in CAMOUFOX_WINDOW_SIZES

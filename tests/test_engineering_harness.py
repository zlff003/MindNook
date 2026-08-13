from app.harness.engineering import SUITE_NAMES, EngineeringHarness


def test_one_click_engineering_harness_covers_all_core_suites():
    report = EngineeringHarness().run()

    assert [suite["name"] for suite in report["suites"]] == list(SUITE_NAMES)
    assert report["summary"]["suite_count"] == 6
    assert report["summary"]["check_count"] >= 20
    assert report["ok"], report

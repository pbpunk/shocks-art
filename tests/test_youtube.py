from app.services.youtube import parse_iso8601_duration


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("PT45M") == 2700


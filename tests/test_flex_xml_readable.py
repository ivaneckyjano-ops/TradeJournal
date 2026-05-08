"""Testy pre IB Flex XML parsovanie."""

from core.flex_xml_readable import normalize_flex_xml_text, parse_flex_xml_string


def test_normalize_prepends_angle_bracket():
    raw = 'FlexQueryResponse queryName="T" type="AF"></FlexQueryResponse>'
    assert normalize_flex_xml_text(raw).startswith("<FlexQueryResponse")


def test_parse_minimal_executions_and_positions():
    xml = """<?xml version="1.0"?>
<FlexQueryResponse queryName="Q" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1" fromDate="20260101" toDate="20260101" whenGenerated="x">
<Trades>
<Trade currency="USD" symbol="SPY" levelOfDetail="EXECUTION" quantity="1" tradePrice="1.5" />
</Trades>
<PriorPeriodPositions>
<PriorPeriodPosition symbol="SPY" date="20260101" price="400" currency="USD" />
</PriorPeriodPositions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""
    buckets, meta = parse_flex_xml_string(xml)
    assert meta["queryName"] == "Q"
    assert len(buckets["executions"]) == 1
    assert buckets["executions"][0].get("symbol") == "SPY"
    assert len(buckets["prior_period_positions"]) == 1

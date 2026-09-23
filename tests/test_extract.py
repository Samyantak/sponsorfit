import json
from unittest.mock import MagicMock

from sponsorfit.corpus.extract import PreparedIssue, evidence_grounding, extract_sponsors, prepare_issue

PAGE = """<html><head><title>t</title><style>p span[style] {color:red}</style></head><body>
<script>{"html":"TOGETHER WITH ACME duplicate copy in JSON"}</script>
<h6>TOGETHER WITH</h6><a href="https://acme.com/?utm_source=news"><img alt="Acme logo" src="x.png"></a>
<p>Acme makes widgets. Try it free.</p>
<p>Today’s story: markets rallied.</p>
<p>Today’s story: markets rallied.</p>
<a href="https://www.news.example.com/p/other">our archive</a>
<a href="https://twitter.com/news">follow us</a>
</body></html>"""


def test_prepare_drops_script_json_and_dedupes_lines():
    issue = prepare_issue(PAGE, "https://news.example.com")
    assert "duplicate copy in JSON" not in issue.text
    assert "p span" not in issue.text
    assert issue.text.count("markets rallied") == 1
    assert "TOGETHER WITH" in issue.text


def test_prepare_keeps_sponsor_links_and_logo_alts_but_not_own_or_social():
    issue = prepare_issue(PAGE, "https://news.example.com")
    assert issue.links == ["(image/no text) -> acme.com"]
    assert issue.alts == ["Acme logo"]


def test_grounding_tolerates_curly_quotes_and_case():
    issue = PreparedIssue(text="Today’s sponsor, Semrush, has a useful example.", links=[], alts=[])
    assert evidence_grounding("today's sponsor, Semrush, has a useful example", issue) == "exact"


def test_grounding_flags_invented_evidence():
    issue = PreparedIssue(text="Markets rallied on Tuesday.", links=[], alts=[])
    assert evidence_grounding("Brought to you by Acme", issue) == "none"


def test_grounding_prefix_when_model_paraphrases_the_tail():
    issue = PreparedIssue(text="This issue is brought to you by our friends at Blitzy! Blitzy builds", links=[], alts=[])
    assert evidence_grounding("This issue is brought to you by our friends at Blitzy, the AI platform", issue) == "prefix"


def _client_returning(text, tokens_in=500):
    client = MagicMock()
    client.call.return_value = MagicMock(text=text, call_id="c1", tokens_in=tokens_in, tokens_out=40, latency_ms=900)
    return client


def _run(client, issue):
    return extract_sponsors(
        client, system_prompt="s", prompt_version="extraction_v1", model="qwen3:8b",
        newsletter="N", issue_url="https://n.com/p/1", issue=issue,
    )


def test_extract_marks_each_sponsor_with_grounding():
    issue = PreparedIssue(text="TOGETHER WITH\nAcme makes widgets.", links=["(image/no text) -> acme.com"], alts=["Acme logo"])
    out = {
        "sponsors": [
            {"name": "Acme", "domain": "acme.com", "evidence": "TOGETHER WITH", "placement": "primary", "confidence": "high"},
            {"name": "Ghost Co", "domain": None, "evidence": "Presented by Ghost Co", "placement": "primary", "confidence": "low"},
        ],
        "excluded": [],
    }
    result = _run(_client_returning(json.dumps(out)), issue)
    assert result["status"] == "ok"
    assert [s["grounding"] for s in result["sponsors"]] == ["exact", "none"]


def test_extract_reports_bad_json_instead_of_crashing():
    result = _run(_client_returning("not json"), PreparedIssue("x", [], []))
    assert result["status"] == "bad_json" and result["sponsors"] == []


def test_extract_flags_silent_context_truncation():
    result = _run(_client_returning('{"sponsors": [], "excluded": []}', tokens_in=16384), PreparedIssue("x", [], []))
    assert result["status"] == "context_truncated"


def test_signal_nearby_separates_ads_from_news_items():
    from sponsorfit.corpus.extract import sponsorship_signal_nearby

    text = (
        "Funding\n" + "Other news filler. " * 30 + "\nSpend management platform Brex secured a $235m credit facility with Citi.\n"
        + "More filler. " * 30 + "\nSPONSORED\nMiddesk helps you verify businesses instantly.\n"
    )
    issue = PreparedIssue(text=text, links=[], alts=[])
    assert not sponsorship_signal_nearby("Spend management platform Brex secured a $235m credit facility", issue)
    assert sponsorship_signal_nearby("Middesk helps you verify businesses instantly", issue)
    assert sponsorship_signal_nearby("Brought to you by Acme", issue)  # label in the evidence itself

from optionsignal.cli import build_parser, main
from optionsignal.models import OptionChain
from tests.test_metrics import NOW, quote


def _chain() -> OptionChain:
    quotes = [
        quote("call", 100, 1.5, volume=50, iv=0.2),
        quote("put", 100, 1.5, volume=50, iv=0.25),
    ]
    return OptionChain(
        symbol="QQQ",
        spot=100.0,
        futures_symbol="NQ=F",
        futures_price=20000.0,
        asof=NOW,
        quotes=quotes,
        source="test",
    )


def test_parser_defaults():
    parser = build_parser()
    args = parser.parse_args(["snapshot", "--json", "--no-save"])
    assert args.symbol == "QQQ"
    assert args.json is True


def test_serve_reads_railway_port(monkeypatch):
    monkeypatch.setenv("PORT", "9090")
    from optionsignal.cli import build_parser as rebuild

    args = rebuild().parse_args(["serve"])
    assert args.port == 9090
    assert args.host == "0.0.0.0"


def test_snapshot_json(monkeypatch, capsys):
    monkeypatch.setattr("optionsignal.cli.fetch_chain", lambda **kwargs: _chain())
    rc = main(["snapshot", "--json", "--no-save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "headline_cppi" in out
    assert "QQQ" in out

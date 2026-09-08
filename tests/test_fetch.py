import pytest

from optionsignal.fetch import fetch_chain
from optionsignal.tasty import FeedConfigError


def test_fetch_chain_rejects_futures_root():
    with pytest.raises(FeedConfigError, match="Yahoo"):
        fetch_chain(symbol="/NQ")
    with pytest.raises(FeedConfigError, match="Yahoo"):
        fetch_chain(symbol="NQ")

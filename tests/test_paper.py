from orbbot.paper import PaperClient


class Real:
    def __init__(self, feeds):
        self.feeds = feeds

    def retrieve_bars(self, cid, start, end, **kw):
        return self.feeds[cid]


def bar(t, px):
    return {"t": t, "o": px, "h": px, "l": px, "c": px, "v": 1}


def test_paper_broker_ignores_confirmation_market_prices():
    real = Real({"MNQ": [bar("2026-10-19T14:00:00+00:00", 20000.0)],
                 "MES": [bar("2026-10-19T14:00:00+00:00", 6800.0)]})
    p = PaperClient(real)
    p.set_contract("MNQ", 0.25, 2.0)
    p.retrieve_bars("MNQ", None, None)
    p.retrieve_bars("MES", None, None)   # must not move the simulated MNQ price
    assert p.last_price == 20000.0

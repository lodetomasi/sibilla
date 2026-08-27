"""Test unita' per il ramo Limitless: edge netto, eleggibilita', naming."""
from datetime import UTC, datetime, timedelta

from collectors.limitless.markets import market_epic, parse_expiry, select_candidates
from intelligence.limitless_pipeline import compute_edge


class Cfg:
    min_price = 0.05
    max_price = 0.95
    min_hours_to_expiry = 1.0


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _mk(mid, yes, vol, hours, tags=()):
    exp_ms = int((NOW + timedelta(hours=hours)).timestamp() * 1000)
    parsed = {"id": mid, "title": f"m{mid}", "yes_price": yes, "no_price": 1 - yes,
              "volume": vol, "categories": ["crypto"], "tags": list(tags), "collateral": "USDC"}
    raw = {"expirationTimestamp": exp_ms, "slug": f"m-{mid}"}
    return parsed, raw


def test_edge_yes_side():
    # modello 0.60 vs ask YES 0.50: edge ~0.085 dopo fee 3%
    out = compute_edge(0.60, 0.48, 0.50, 300)
    assert out["side"] == "YES"
    assert abs(float(out["edge"]) - (0.60 - 0.50 - 0.03 * 0.50)) < 1e-9


def test_edge_no_side():
    # modello 0.20 vs bid YES 0.45 -> ask NO 0.55: edge NO = 0.80-0.55-fee
    out = compute_edge(0.20, 0.45, 0.47, 300)
    assert out["side"] == "NO"
    assert abs(float(out["edge"]) - (0.80 - 0.55 - 0.03 * 0.55)) < 1e-9


def test_edge_fee_kills_marginal():
    out = compute_edge(0.52, 0.49, 0.51, 300)
    assert float(out["edge"]) < 0.02  # edge lordo 1pt sparisce coi costi


def test_select_candidates_filters_noise_and_extremes():
    markets = [
        _mk("1", 0.55, 100_000, 48),               # ok, top priority (0.50 esatto = placeholder AMM, escluso)
        _mk("2", 0.985, 500_000, 48),              # prezzo estremo -> fuori
        _mk("3", 0.40, 50_000, 0.2),               # scade tra 12 minuti -> fuori
        _mk("4", 0.30, 80_000, 24, tags=["5 min"]),  # rumore scalping -> fuori
        _mk("5", 0.60, 1_000, 24 * 10),            # ok, volume basso
    ]
    got = select_candidates(markets, Cfg(), NOW)
    ids = [c.market_id for c in got]
    assert ids == ["1", "5"]
    assert got[0].epic == market_epic("1") == "LMTS:1"


def test_parse_expiry_ms_and_iso():
    assert parse_expiry({"expirationTimestamp": 1790000000000}).year >= 2026
    assert parse_expiry({"expirationDate": "2026-09-01T00:00:00Z"}).month == 9
    assert parse_expiry({}) is None


def test_min_shares_slippage():
    from execution.limitless.onchain import min_shares
    assert min_shares(1_000_000, 300) == 970_000
    assert min_shares(0, 300) == 0


def test_pair_bids_sum_and_bounds():
    from execution.limitless.maker import pair_bids
    for mid in (0.1, 0.3, 0.5, 0.75, 0.93):
        b_yes, b_no = pair_bids(mid, 0.955)
        assert abs((b_yes + b_no) - 0.955) < 0.002
        assert 0 < b_yes < 1 and 0 < b_no < 1


def test_completion_cap_locks_profit_only():
    from execution.limitless.maker import completion_cap
    assert completion_cap(0.888) == 0.102  # fill YES a 0.888 -> NO max 0.102 (tot 0.99)
    assert completion_cap(0.05) == 0.94
    assert completion_cap(0.99) == 0.0     # nessun margine -> nessun inseguimento
    assert completion_cap(1.2) == 0.0      # mai negativo


def test_digital_p_up_bounds_and_monotonicity():
    from execution.limitless.maker import digital_p_up
    assert digital_p_up(100.0, 100.0, 0.001, 1800) == 0.5     # at-the-money
    assert digital_p_up(101.0, 100.0, 0.001, 1800) > 0.9      # +1% sopra open, poca vol residua
    assert digital_p_up(99.0, 100.0, 0.001, 1800) < 0.1
    assert 0.01 <= digital_p_up(150.0, 100.0, 0.001, 60) <= 0.99  # clamp agli estremi
    # meno tempo residuo => digitale piu' decisa; piu' tempo => torna verso 0.5
    assert digital_p_up(101.0, 100.0, 0.001, 60) > digital_p_up(101.0, 100.0, 0.001, 3600) > 0.5
    assert digital_p_up(0.0, 100.0, 0.001, 600) == 0.5        # input degenere -> neutro


def test_elo_1x2_from_clubelo_row():
    from intelligence.limitless_pipeline import elo_1x2
    # riga reale ClubElo (Barcelona-Bilbao 2026-08-27, troncata alle colonne GD)
    row = {"GD<-5": "0.0", "GD=-5": "0.0", "GD=-4": "0.0001", "GD=-3": "0.0032", "GD=-2": "0.0141",
           "GD=-1": "0.0512", "GD=0": "0.1261", "GD=1": "0.238", "GD=2": "0.2163", "GD=3": "0.1637",
           "GD=4": "0.1028", "GD=5": "0.0527", "GD>5": "0.0317", "R:0-0": "0.0245"}
    ph, pd, pa = elo_1x2(row)
    assert abs(ph + pd + pa - 1.0) < 0.01     # distribuzione completa
    assert ph > 0.7 and pa < 0.1              # Barcellona nettamente favorito
    assert pd == 0.1261


def test_elo_win_prob_and_ta_row_regex():
    import re
    from intelligence.limitless_pipeline import TA_ROW_RE, elo_win_prob
    # simmetria e ordini di grandezza
    assert abs(elo_win_prob(2321.9, 2146.8) + elo_win_prob(2146.8, 2321.9) - 1.0) < 1e-9
    assert 0.70 < elo_win_prob(2321.9, 2146.8) < 0.80   # Sinner vs Alcaraz ~73%
    assert elo_win_prob(1500, 1500) == 0.5
    # regex sulla riga HTML reale di Tennis Abstract
    row = ('<tr><td align="right">1</td><td><a href="https://www.tennisabstract.com/cgi-bin/'
           'player.cgi?p=JannikSinner">Jannik&nbsp;Sinner</a></td><td align="right">24.8</td>'
           '<td align="right">2321.9</td><td></td></tr>')
    m = re.findall(TA_ROW_RE, row)
    assert m == [("Jannik&nbsp;Sinner", "2321.9")]


def test_held_edge_news_exit_rule():
    from intelligence.limitless_pipeline import held_edge
    # NO comprate a p(yes)=0.25; il giudice ora dice p=0.50 col book a 0.50/0.52 -> edge morto
    assert held_edge(0.50, "NO", 0.50, 0.52) < 0
    assert held_edge(0.20, "NO", 0.50, 0.52) > 0     # NO ancora value: tenere
    assert held_edge(0.70, "YES", 0.55, 0.57) > 0
    assert held_edge(0.40, "YES", 0.55, 0.57) < 0    # YES senza edge: vendere

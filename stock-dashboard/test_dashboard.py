from datetime import UTC, datetime

import pytest

from market_data import Bar, Instrument, MarketError, MarketStore, TradingCalendar, YahooFinance, normalize_symbol
from prediction import NextSessionForecaster
from server import Dashboard


def test_symbol_normalization_rejects_path_segments():
    assert normalize_symbol(' brk.b ') == 'BRK-B'
    with pytest.raises(MarketError, match='invalid_symbol'):
        normalize_symbol('../AAPL')


def test_calendar_handles_thanksgiving_and_early_close():
    calendar = TradingCalendar()
    assert calendar.next_session('2026-11-25') == '2026-11-27'
    assert calendar.completed_session(datetime(2026, 11, 27, 18, 19, tzinfo=UTC)) == '2026-11-25'
    assert calendar.completed_session(datetime(2026, 11, 27, 18, 20, tzinfo=UTC)) == '2026-11-27'


def test_provider_skips_missing_close_and_adjusts_ohlc():
    payload = {'chart': {'result': [{
        'meta': {'instrumentType': 'EQUITY', 'currency': 'USD', 'exchangeTimezoneName': 'America/New_York',
                 'symbol': 'AAPL', 'shortName': 'Apple Inc.'},
        'timestamp': [1795444200, 1795530600],
        'indicators': {
            'quote': [{'open': [100, 105], 'high': [110, 112], 'low': [95, 102], 'close': [105, None],
                       'volume': [50, 80]}],
            'adjclose': [{'adjclose': [52.5, None]}],
        },
    }]}}
    instrument, bars = YahooFinance.parse('AAPL', payload)
    assert instrument.name == 'Apple Inc.'
    assert bars == [Bar('2026-11-23', 50, 55, 47.5, 52.5, 50)]


def test_exchange_rows_revision_and_idempotent_cache(tmp_path):
    store = MarketStore(tmp_path / 'market.sqlite3')
    instrument = Instrument('AAPL', 'Apple', 'EQUITY')
    bar = Bar('2026-11-23', 100, 104, 98, 102, 50)
    store.save(instrument, [bar], 1)
    store.save(instrument, [bar], 2)
    assert store.sync_status(instrument.symbol) == (2, 1)
    assert store.bars(instrument.symbol, '2026-11-23') == [bar]
    changed = Bar('2026-11-23', 100, 104, 98, 103, 60)
    store.save(instrument, [changed], 3)
    assert store.sync_status(instrument.symbol) == (3, 2)
    assert store.bars(instrument.symbol, '2026-11-23') == [changed]


def test_service_filters_open_bar_and_marks_stale_cache(tmp_path):
    calendar = TradingCalendar()

    class Provider:
        fail = False

        def fetch(self, symbol):
            if self.fail:
                raise MarketError('provider_unavailable')
            start = calendar.calendar.schedule(start_date='2026-01-02', end_date='2026-11-24')
            bars = [
                Bar(day.date().isoformat(), 100, 102 + (index % 7), 98, 101, 1000)
                for index, day in enumerate(start.index)
            ]
            return Instrument(symbol, 'Apple', 'EQUITY'), bars

    provider = Provider()
    now = datetime(2026, 11, 24, 20, 0, tzinfo=UTC)
    service = Dashboard(tmp_path / 'market.sqlite3', provider=provider, calendar=calendar, now=lambda: now)
    prediction = service.forecast('AAPL')
    assert prediction['latestSession'] == '2026-11-23'
    assert prediction['targetSession'] == '2026-11-24'
    assert prediction['forecast']['trainingEnd'] < prediction['latestSession']
    for tree in prediction['forecast']['trees']:
        assert tree['nodes'][0]['active']
        assert len([node for node in tree['nodes'] if node['active'] and 'feature' not in node]) == 1

    now = datetime(2026, 11, 24, 21, 21, tzinfo=UTC)
    provider.fail = True
    cached = service.forecast('AAPL', force=True)
    assert cached['sourceState'] == 'delayed'
    assert cached['expectedSession'] == '2026-11-24'
    assert cached['latestSession'] == '2026-11-23'
    assert cached['forecast']['amplitude'] == prediction['forecast']['amplitude']


def test_model_declines_to_train_on_short_history():
    bars = [Bar('2026-11-23', 100, 104, 98, 102, 50)]
    assert NextSessionForecaster().forecast(bars) is None


def test_alpaca_parses_adjusted_daily_bars():
    from alpaca_data import AlpacaDaily

    payload = {'bars': {'AAPL': [
        {'t': '2026-11-24T05:00:00Z', 'o': 100, 'h': 105, 'l': 98, 'c': 102, 'v': 2000},
        {'t': '2026-11-25T05:00:00Z', 'o': 100, 'h': 99, 'l': 98, 'c': 102, 'v': 2000},
    ]}}
    assert list(AlpacaDaily.parse(payload, 'AAPL')) == [Bar('2026-11-24', 100, 105, 98, 102, 2000)]


def test_alpaca_credentials_require_owner_only_permissions(tmp_path):
    from alpaca_data import local_credentials

    secrets = tmp_path / 'secrets.json'
    assert local_credentials(secrets) is None
    secrets.write_text('{"alpaca":{"key_id":"example-id","secret_key":"example-secret"}}')
    secrets.chmod(0o644)
    with pytest.raises(MarketError, match='alpaca_secret_permissions'):
        local_credentials(secrets)
    secrets.chmod(0o600)
    assert local_credentials(secrets) == ('example-id', 'example-secret')

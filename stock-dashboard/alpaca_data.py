"""Read-only adapter and local configuration for Alpaca market data."""

from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import stat
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from market_data import Bar, Instrument, MarketError, NEW_YORK, normalize_symbol


SECRETS_FILE = Path(__file__).resolve().parent / 'local-secrets' / 'notes' / 'project-secrets.json'


def local_credentials(path=SECRETS_FILE):
    if not path.exists():
        return None
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MarketError('alpaca_secret_permissions', 400)
    try:
        config = json.loads(path.read_text()).get('alpaca')
        if config is None:
            return None
        key_id, secret_key = config['key_id'], config['secret_key']
        if not all(isinstance(item, str) and item.strip() for item in (key_id, secret_key)):
            raise ValueError
        return key_id.strip(), secret_key.strip()
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        raise MarketError('alpaca_secret_config', 400) from error


class AlpacaDaily:
    name = 'Alpaca SIP'
    url = 'https://data.alpaca.markets/v2/stocks/bars'

    def __init__(self, credentials, lookup=lambda symbol: None):
        self.key_id, self.secret_key = credentials
        self.lookup = lookup

    def fetch(self, symbol):
        symbol = normalize_symbol(symbol)
        end = datetime.now(UTC) - timedelta(minutes=20)
        params = {
            'symbols': symbol, 'timeframe': '1Day', 'start': (end - timedelta(days=731)).isoformat(),
            'end': end.isoformat(), 'adjustment': 'all', 'feed': 'sip', 'limit': 10000,
        }
        bars = {}
        while True:
            payload = self.request(params)
            bars.update({bar.session: bar for bar in self.parse(payload, symbol)})
            page = payload.get('next_page_token')
            if not page:
                break
            if page == params.get('page_token'):
                raise MarketError('invalid_provider_data')
            params['page_token'] = page
        if not bars:
            raise MarketError('symbol_not_found', 404)
        # The bars endpoint supplies prices. Existing catalog metadata supplies the display name.
        instrument = self.lookup(symbol) or Instrument(symbol, symbol, 'US_EQUITY')
        return instrument, sorted(bars.values(), key=lambda bar: bar.session)

    def request(self, params):
        request = Request(self.url + '?' + urlencode(params), headers={'Accept': 'application/json'})
        request.add_unredirected_header('APCA-API-KEY-ID', self.key_id)
        request.add_unredirected_header('APCA-API-SECRET-KEY', self.secret_key)
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read(2_000_000))
        except HTTPError as error:
            if error.code == 401:
                raise MarketError('alpaca_auth', 401) from None
            if error.code == 403:
                raise MarketError('alpaca_feed_permission', 403) from None
            raise MarketError('provider_unavailable') from None
        except (OSError, ValueError):
            raise MarketError('provider_unavailable') from None
        if not isinstance(payload, dict) or not isinstance(payload.get('bars'), dict):
            raise MarketError('invalid_provider_data')
        return payload

    @staticmethod
    def parse(payload, symbol):
        for record in payload['bars'].get(symbol, []):
            try:
                session = datetime.fromisoformat(record['t']).astimezone(NEW_YORK).date().isoformat()
                open_, high, low, close = (float(record[key]) for key in ('o', 'h', 'l', 'c'))
                volume = int(record['v'])
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            if (
                all(math.isfinite(value) and value > 0 for value in (open_, high, low, close))
                and low <= min(open_, close) <= max(open_, close) <= high
                and volume >= 0
            ):
                yield Bar(session, open_, high, low, close, volume)

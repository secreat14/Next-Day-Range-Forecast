"""Serve the HTML dashboard and its same-origin live forecasting API."""

from argparse import ArgumentParser
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import RLock
from urllib.parse import parse_qs, urlsplit

from alpaca_data import AlpacaDaily, local_credentials
from market_data import MarketError, MarketStore, TradingCalendar, YahooFinance, normalize_symbol
from prediction import NextSessionForecaster


ROOT = Path(__file__).resolve().parent
PAGE = ROOT.parent / 'stock-amplitude-forecast.html'
SYNC_INTERVAL = timedelta(minutes=5)
QUOTE_DAYS = 60


class Dashboard:
    def __init__(self, database, provider=None, calendar=None, forecaster=None, now=None):
        self.store = MarketStore(database)
        self.provider = provider or YahooFinance()
        self.calendar = calendar or TradingCalendar()
        self.forecaster = forecaster or NextSessionForecaster()
        self.now = now or (lambda: datetime.now(UTC))
        self.lock = RLock()
        self.models = {}
        self.last_failed_sync = {}

    def catalog(self):
        return [asdict(instrument) for instrument in self.store.instruments()]

    def add(self, value):
        symbol = normalize_symbol(value)
        with self.lock:
            existing = self.store.instrument(symbol)
            if existing:
                return asdict(existing)
            instrument, bars = self.provider.fetch(symbol)
            now = self.now()
            completed = self.calendar.completed_session(now)
            self.store.save(instrument, [bar for bar in bars if bar.session <= completed], now.timestamp())
            return asdict(instrument)

    def forecast(self, value, force=False):
        symbol = normalize_symbol(value)
        with self.lock:
            instrument = self.store.instrument(symbol)
            if instrument is None:
                raise MarketError('symbol_not_found', 404)
            now = self.now()
            expected = self.calendar.completed_session(now)
            status = self.store.sync_status(symbol)
            failed_at = self.last_failed_sync.get(symbol)
            recent_failure = failed_at is not None and now - failed_at < SYNC_INTERVAL
            should_sync = force or (
                not recent_failure and (status is None or now.timestamp() - status[0] >= SYNC_INTERVAL.total_seconds())
            )
            if should_sync:
                try:
                    instrument, received = self.provider.fetch(symbol)
                    now = self.now()
                    expected = self.calendar.completed_session(now)
                    completed = [bar for bar in received if bar.session <= expected]
                    if not completed:
                        raise MarketError('invalid_provider_data')
                    self.store.save(instrument, completed, now.timestamp())
                except MarketError:
                    self.last_failed_sync[symbol] = self.now()
                    if status is None:
                        raise
                else:
                    self.last_failed_sync.pop(symbol, None)
                    status = self.store.sync_status(symbol)
            if status is None:
                raise MarketError('provider_unavailable')

            bars = self.store.bars(symbol, expected)
            if not bars:
                raise MarketError('invalid_provider_data')
            latest = bars[-1].session
            fresh_state = 'delayed' if latest < expected else 'cached' if symbol in self.last_failed_sync else 'current'
            target = self.calendar.next_session(latest)
            cache_key = (status[1], latest)
            cached = self.models.get(symbol)
            if cached is None or cached[0] != cache_key:
                actual_sessions = [bar.session for bar in bars[-23:]]
                expected_sessions = self.calendar.recent_sessions(latest, len(actual_sessions))
                if actual_sessions != expected_sessions:
                    model, reason = None, 'missing_sessions'
                else:
                    model = self.forecaster.forecast(bars)
                    reason = None if model is not None else 'insufficient_history'
                self.models[symbol] = (cache_key, model, reason)
            _, model, reason = self.models[symbol]
            recent = bars[-(QUOTE_DAYS + 1):]
            quote_bars = [
                {'date': bar.session, 'values': bar.quote(previous.close)}
                for previous, bar in zip(recent, recent[1:])
            ]
            return {
                'instrument': asdict(instrument),
                'provider': getattr(self.provider, 'name', 'Yahoo Finance'),
                'sourceState': fresh_state,
                'dataRevision': status[1],
                'syncedAt': datetime.fromtimestamp(status[0], UTC).isoformat(),
                'expectedSession': expected,
                'latestSession': latest,
                'targetSession': target,
                'currency': 'USD',
                'bar': quote_bars[-1] if quote_bars else None,
                'quotes': quote_bars,
                'forecast': model,
                'reason': reason,
            }


class RequestHandler(BaseHTTPRequestHandler):
    def write(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def write_json(self, value, status=200):
        self.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8', status)

    def api(self, action):
        try:
            self.write_json(action())
        except MarketError as error:
            self.write_json({'error': error.code}, error.status)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in {'/', '/stock-amplitude-forecast.html'}:
            self.write(PAGE.read_bytes(), 'text/html; charset=utf-8')
        elif url.path == '/api/instruments':
            self.api(self.server.dashboard.catalog)
        elif url.path == '/api/forecast':
            symbol = parse_qs(url.query).get('symbol', [None])[0]
            self.api(lambda: self.server.dashboard.forecast(symbol))
        else:
            self.write_json({'error': 'not_found'}, 404)

    def read_symbol(self):
        try:
            if self.headers.get_content_type() != 'application/json':
                raise ValueError
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                raise ValueError
            return json.loads(self.rfile.read(length))['symbol']
        except (KeyError, TypeError, ValueError) as error:
            raise MarketError('invalid_request', 400) from error

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == '/api/instruments':
            self.api(lambda: self.server.dashboard.add(self.read_symbol()))
        elif path == '/api/refresh':
            self.api(lambda: self.server.dashboard.forecast(self.read_symbol(), force=True))
        else:
            self.write_json({'error': 'not_found'}, 404)


def main():
    parser = ArgumentParser(description='Run the local stock forecast dashboard.')
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--database', type=Path, default=ROOT / '.data' / 'market.sqlite3')
    parser.add_argument('--provider', choices=('auto', 'yahoo', 'alpaca'), default='auto')
    arguments = parser.parse_args()
    try:
        credentials = local_credentials() if arguments.provider != 'yahoo' else None
    except MarketError as error:
        parser.error(error.code)
    if arguments.provider == 'alpaca' and credentials is None:
        parser.error('Alpaca Paper credentials have not been configured.')
    dashboard = Dashboard(arguments.database)
    if credentials:
        yahoo_catalog = dashboard.store
        alpaca_db = arguments.database.with_name(arguments.database.stem + '-alpaca.sqlite3')
        dashboard = Dashboard(alpaca_db, provider=AlpacaDaily(credentials, yahoo_catalog.instrument))
        dashboard.store.merge_catalog(yahoo_catalog.instruments())
    with ThreadingHTTPServer(('127.0.0.1', arguments.port), RequestHandler) as server:
        server.dashboard = dashboard
        print(f'http://127.0.0.1:{server.server_port}/', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()

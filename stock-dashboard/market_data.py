"""Fetch completed U.S. equity sessions and keep a local SQLite cache."""

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas_market_calendars as market_calendars


DEFAULT_INSTRUMENTS = (
    ('AAPL', 'Apple', 'EQUITY'),
    ('MSFT', 'Microsoft', 'EQUITY'),
    ('AMZN', 'Amazon', 'EQUITY'),
    ('SPY', 'S&P 500 ETF', 'ETF'),
    ('QQQ', 'Nasdaq-100 ETF', 'ETF'),
    ('IWM', 'Russell 2000 ETF', 'ETF'),
)
NEW_YORK = ZoneInfo('America/New_York')
CLOSE_BUFFER = timedelta(minutes=20)
SYMBOL = re.compile(r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)?')


class MarketError(Exception):
    """An error safe to return to the dashboard."""

    def __init__(self, code, status=503):
        super().__init__(code)
        self.code = code
        self.status = status


def normalize_symbol(value):
    symbol = value.strip().upper().replace('.', '-') if isinstance(value, str) else ''
    if len(symbol) > 12 or not SYMBOL.fullmatch(symbol):
        raise MarketError('invalid_symbol', 400)
    return symbol


@dataclass(frozen=True)
class Bar:
    session: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    def quote(self, previous_close):
        return [self.open, self.high, self.low, self.close, self.volume, previous_close]


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    kind: str


class TradingCalendar:
    def __init__(self):
        self.calendar = market_calendars.get_calendar('NYSE')

    @lru_cache(maxsize=24)
    def schedule(self, session_date):
        return self.calendar.schedule(
            start_date=session_date - timedelta(days=95),
            end_date=session_date + timedelta(days=15),
        )

    def completed_session(self, now):
        schedule = self.schedule(now.astimezone(NEW_YORK).date())
        completed = schedule[schedule.market_close + CLOSE_BUFFER <= now]
        return completed.index[-1].date().isoformat()

    def next_session(self, session):
        schedule = self.schedule(date.fromisoformat(session))
        dates = schedule.index.date
        return next(day.isoformat() for day in dates if day.isoformat() > session)

    def recent_sessions(self, session, count):
        schedule = self.schedule(date.fromisoformat(session))
        dates = [day.date().isoformat() for day in schedule.index if day.date().isoformat() <= session]
        return dates[-count:]


class YahooFinance:
    """Adapter for Yahoo Finance's public daily chart response."""

    name = 'Yahoo Finance'
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/'

    def fetch(self, symbol):
        query = urlencode({'range': '2y', 'interval': '1d', 'includePrePost': 'false'})
        request = Request(
            self.url + symbol + '?' + query,
            headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'},
        )
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read(2_000_000))
        except HTTPError as error:
            if error.code == 404:
                raise MarketError('symbol_not_found', 404) from error
            raise MarketError('provider_unavailable') from error
        except (OSError, URLError, ValueError) as error:
            raise MarketError('provider_unavailable') from error
        try:
            return self.parse(symbol, payload)
        except (KeyError, TypeError, IndexError, ValueError) as error:
            raise MarketError('invalid_provider_data') from error

    @staticmethod
    def parse(symbol, payload):
        chart = payload['chart']
        if not chart['result']:
            raise MarketError('symbol_not_found', 404)
        result = chart['result'][0]
        meta = result['meta']
        kind = meta.get('instrumentType')
        if kind not in {'EQUITY', 'ETF'} or meta.get('currency') != 'USD' or meta.get('exchangeTimezoneName') != NEW_YORK.key:
            raise MarketError('unsupported_symbol', 400)
        if meta.get('symbol') != symbol:
            raise MarketError('symbol_not_found', 404)
        instrument = Instrument(symbol, (meta.get('shortName') or meta.get('longName') or symbol)[:120], kind)
        indicators = result['indicators']
        prices = indicators['quote'][0]
        adjustments = indicators['adjclose'][0]['adjclose']
        bars = {}
        for timestamp, open_, high, low, close, volume, adjusted in zip(
            result['timestamp'], prices['open'], prices['high'], prices['low'], prices['close'], prices['volume'], adjustments,
            strict=True,
        ):
            market_prices = (open_, high, low, close, adjusted)
            if any(value is None or not math.isfinite(value) or value <= 0 for value in market_prices):
                continue
            if high < max(open_, close, low) or low > min(open_, close) or volume is None or volume < 0:
                continue
            factor = adjusted / close
            session = datetime.fromtimestamp(timestamp, UTC).astimezone(NEW_YORK).date().isoformat()
            bars[session] = Bar(session, open_ * factor, high * factor, low * factor, adjusted, int(volume))
        if not bars:
            raise MarketError('invalid_provider_data')
        return instrument, sorted(bars.values(), key=lambda bar: bar.session)


class MarketStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as database, database:
            database.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS instruments (
                    symbol TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL REFERENCES instruments(symbol),
                    session TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
                    low REAL NOT NULL, close REAL NOT NULL, volume INTEGER NOT NULL,
                    PRIMARY KEY (symbol, session)
                );
                CREATE TABLE IF NOT EXISTS sync_status (
                    symbol TEXT PRIMARY KEY REFERENCES instruments(symbol),
                    fetched_at REAL NOT NULL, revision INTEGER NOT NULL
                );
            ''')
            database.executemany('INSERT OR IGNORE INTO instruments VALUES (?, ?, ?)', DEFAULT_INSTRUMENTS)

    def connect(self):
        database = sqlite3.connect(self.path, timeout=8)
        database.execute('PRAGMA foreign_keys=ON')
        return database

    def instruments(self):
        with closing(self.connect()) as database:
            return [Instrument(*row) for row in database.execute('SELECT symbol, name, kind FROM instruments ORDER BY symbol')]

    def merge_catalog(self, instruments):
        with closing(self.connect()) as database, database:
            database.executemany('INSERT OR IGNORE INTO instruments VALUES (?, ?, ?)', [
                (instrument.symbol, instrument.name, instrument.kind) for instrument in instruments
            ])

    def instrument(self, symbol):
        with closing(self.connect()) as database:
            row = database.execute('SELECT symbol, name, kind FROM instruments WHERE symbol = ?', (symbol,)).fetchone()
            return Instrument(*row) if row else None

    def sync_status(self, symbol):
        with closing(self.connect()) as database:
            return database.execute('SELECT fetched_at, revision FROM sync_status WHERE symbol = ?', (symbol,)).fetchone()

    def save(self, instrument, bars, fetched_at):
        with closing(self.connect()) as database, database:
            database.execute('''
                INSERT INTO instruments VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, kind = excluded.kind
            ''', (instrument.symbol, instrument.name, instrument.kind))
            changed = database.executemany('''
                INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, session) DO UPDATE SET open = excluded.open, high = excluded.high,
                    low = excluded.low, close = excluded.close, volume = excluded.volume
                WHERE bars.open != excluded.open OR bars.high != excluded.high OR bars.low != excluded.low
                    OR bars.close != excluded.close OR bars.volume != excluded.volume
            ''', [
                (instrument.symbol, bar.session, bar.open, bar.high, bar.low, bar.close, bar.volume)
                for bar in bars
            ]).rowcount
            database.execute('''
                INSERT INTO sync_status VALUES (?, ?, 1)
                ON CONFLICT(symbol) DO UPDATE SET fetched_at = excluded.fetched_at, revision = revision + ?
            ''', (instrument.symbol, fetched_at, int(changed > 0)))

    def bars(self, symbol, through, limit=505):
        with closing(self.connect()) as database:
            rows = database.execute('''
                SELECT session, open, high, low, close, volume FROM bars
                WHERE symbol = ? AND session <= ? ORDER BY session DESC LIMIT ?
            ''', (symbol, through, limit)).fetchall()
            return [Bar(*row) for row in reversed(rows)]

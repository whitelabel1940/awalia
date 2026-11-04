"""CO1 global accounting in the main DB. Foreground recording has no DB I/O."""
from datetime import timedelta
import math
import sqlite3
from threading import Lock

from cbs import clock

PURPOSES = {'chat': '会話', 'memory': '記憶の整理', 'autonomy': '自主活動',
            'media': '写真と声', 'import': '取り込み', 'welcome': '迎える手順', 'other': 'その他'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_calls (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, role TEXT NOT NULL, purpose TEXT NOT NULL,
 model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL, usd REAL
);
CREATE INDEX IF NOT EXISTS cost_calls_at ON cost_calls(at);
CREATE TABLE IF NOT EXISTS cost_months (
 month TEXT NOT NULL, purpose TEXT NOT NULL, calls INTEGER NOT NULL, unknown INTEGER NOT NULL,
 usd REAL NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
 PRIMARY KEY(month,purpose)
);
CREATE TABLE IF NOT EXISTS cost_notices (month TEXT PRIMARY KEY, seen INTEGER NOT NULL DEFAULT 0);
"""


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def tokens(data):
    def n(*keys):
        return int(next((number(data[k]) for k in keys if number(data.get(k)) is not None), 0))
    details = data.get('input_tokens_details') or data.get('prompt_tokens_details') or {}
    if not isinstance(details, dict):
        details = {}
    read = n('cache_read_input_tokens', 'cached_content_token_count') or int(number(details.get('cached_tokens')) or 0)
    write = n('cache_creation_input_tokens') or int(number(details.get('cache_write_tokens')) or 0)
    inp = n('input_tokens', 'prompt_tokens', 'prompt_token_count')
    # Anthropic input_tokens excludes cache reads/writes; the other APIs include them.
    if 'cache_read_input_tokens' in data or 'cache_creation_input_tokens' in data:
        inp += read + write
    out = n('output_tokens', 'completion_tokens', 'candidates_token_count')
    out += n('thoughts_token_count')  # Gemini bills thinking separately in usage.
    return inp, out, read, write


def estimate(provider, model, data, config):
    data = data if isinstance(data, dict) else {}
    counts = tokens(data)
    if provider == 'local':
        return counts, 0.0
    if provider == 'openrouter':
        return counts, number(data.get('cost'))
    price = config.prices.get(model)
    # Missing usage or a missing cache rate is unknown, never a false zero.
    if price is None or not any(number(data.get(k)) is not None for k in ('input_tokens', 'prompt_tokens', 'prompt_token_count')):
        return counts, None
    inp, out, read, write = counts
    if read + write > inp:
        return counts, None
    if (read and price.cache_read_usd is None) or (write and price.cache_write_usd is None):
        return counts, None
    total = (max(0, inp-read-write)*price.input_usd + out*price.output_usd
             + read*(price.cache_read_usd or 0) + write*(price.cache_write_usd or 0)) / 1_000_000
    return counts, total


class CostRecorder:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.lock, self.flush_lock = Lock(), Lock()
        self.pending, self.scheduled = [], False
        with store.connect() as db:
            self.month_totals = dict(db.execute("SELECT substr(at,1,7),sum(COALESCE(usd,0)) FROM cost_calls GROUP BY 1"))
            for month, amount in db.execute('SELECT month,sum(usd) FROM cost_months GROUP BY month'):
                self.month_totals[month] = self.month_totals.get(month, 0) + amount
            self.notices = dict(db.execute('SELECT month,seen FROM cost_notices'))

    def record(self, provider, model, role, purpose, data):
        counts, amount = estimate(provider, model, data or {}, self.config)
        at = clock.iso()
        with self.lock:
            self.month_totals[at[:7]] = self.month_totals.get(at[:7], 0) + (amount or 0)
            self.pending.append((at, role, purpose if purpose in PURPOSES else 'other', model, *counts, amount))
        self.schedule()

    def schedule(self):
        from cbs.steward import submit
        with self.lock:
            if self.scheduled:
                return
            self.scheduled = True
        submit(self.store, self.flush)

    def exceeded(self):
        with self.lock:
            guide = self.config.monthly_guide_usd
            return guide is not None and self.month_totals.get(clock.iso()[:7], 0) > guide

    def paused(self):
        return self.config.pause_autonomy and self.exceeded()

    def flush(self):
        # Only background workers/settings/shutdown call this. Never wait for a
        # foreground SQLite writer; keep the batch for the next maintenance pass.
        with self.flush_lock:
            with self.lock:
                batch, self.pending = self.pending, []
                notices = dict(self.notices)
                self.scheduled = False
            try:
                with sqlite3.connect(self.store.path, timeout=0) as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.executemany('INSERT INTO cost_calls(at,role,purpose,model,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,usd) VALUES (?,?,?,?,?,?,?,?,?)', batch)
                    db.executemany('INSERT INTO cost_notices VALUES (?,?) ON CONFLICT(month) DO UPDATE SET seen=max(seen,excluded.seen)', notices.items())
                    cutoff = (clock.now()-timedelta(days=self.config.detail_days)).isoformat()
                    db.execute('''INSERT INTO cost_months
                        SELECT substr(at,1,7),purpose,count(*),sum(usd IS NULL),sum(COALESCE(usd,0)),
                        sum(input_tokens),sum(output_tokens),sum(cache_read_tokens),sum(cache_write_tokens)
                        FROM cost_calls WHERE at<? GROUP BY 1,2
                        ON CONFLICT(month,purpose) DO UPDATE SET
                        calls=calls+excluded.calls,unknown=unknown+excluded.unknown,usd=usd+excluded.usd,
                        input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,
                        cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens''', (cutoff,))
                    db.execute('DELETE FROM cost_calls WHERE at<?', (cutoff,))
            except sqlite3.Error:
                with self.lock:
                    self.pending = batch + self.pending

    def notice(self, *, consume=False):
        month = clock.iso()[:7]
        with self.lock:
            guide = self.config.monthly_guide_usd
            if guide is None or self.month_totals.get(month, 0) <= guide or self.notices.get(month) == 1:
                return None
            self.notices[month] = int(consume)
        self.schedule()
        return {'month': month, 'paused': self.paused()}

    def summary(self):
        self.flush()
        today = clock.now().date().isoformat()
        month = today[:7]
        previous = (clock.now().replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
        with self.flush_lock:
            with self.store.connect() as db:
                rows = [dict(r) for r in db.execute('''SELECT substr(at,1,7) month,substr(at,1,10) day,purpose,
                    count(*) calls,sum(usd IS NULL) unknown,sum(COALESCE(usd,0)) usd FROM cost_calls GROUP BY 1,2,3''')]
                rows += [dict(r) for r in db.execute("SELECT month,'' day,purpose,calls,unknown,usd FROM cost_months")]
                turns = dict(db.execute("SELECT substr(created_at,1,7),count(*) FROM turns WHERE status='completed' AND kind='conversation' GROUP BY 1"))
            # Include a batch deferred because another writer was active.
            with self.lock:
                rows += [{'month': r[0][:7], 'day': r[0][:10], 'purpose': r[2], 'calls': 1,
                          'unknown': int(r[-1] is None), 'usd': r[-1] or 0} for r in self.pending]
        def total(selected):
            result = {key: sum(r[key] for r in selected) for key in ('calls','unknown','usd')}
            result['purposes'] = [{'id': p, 'label': label, **{key: sum(r[key] for r in selected if r['purpose']==p)
                                   for key in ('calls','unknown','usd')}} for p,label in PURPOSES.items()]
            return result
        periods = {'today': total([r for r in rows if r['day']==today]),
                   'month': total([r for r in rows if r['month']==month]),
                   'previous': total([r for r in rows if r['month']==previous])}
        periods['month']['turns'] = turns.get(month, 0)
        periods['month']['per_turn_usd'] = periods['month']['usd']/turns[month] if turns.get(month) else None
        return {**periods, 'currency': 'USD', 'paused': self.paused()}


def bind(client, store, config):
    if not hasattr(store, 'costs'):
        store.costs = CostRecorder(store, config.costs)
    store.costs.config = config.costs
    from cbs.llm.failures import FailureState
    if not hasattr(store, 'llm_failures'):
        store.llm_failures = FailureState(store)
    client.failures = store.llm_failures
    client.costs, client._cost_config = store.costs, config
    return client

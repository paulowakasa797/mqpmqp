"""A frozen, symmetric trend-pullback hypothesis; not a fitted profit model."""
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
import math
from statistics import median

STEP = 300_000


@dataclass(frozen=True)
class Rules:
    history_bars: int = 180
    regime_interval_ms: int = 900_000
    ema_fast: int = 8
    ema_slow: int = 21
    regime_slope_bars: int = 3
    atr_bars: int = 14
    pivot_left: int = 2
    pivot_right: int = 2
    minimum_pivot_age_bars: int = 3
    maximum_pivot_age_bars: int = 10
    impulse_base_bars: int = 12
    minimum_impulse_atr: float = 2.0
    retrace_min: float = .25
    retrace_max: float = .60
    confirmation_atr: float = .10
    stop_buffer_atr: float = .20
    min_stop_atr: float = .50
    max_stop_atr: float = 2.0
    target_buffer_atr: float = .10
    entry_buffer_atr: float = .10
    minimum_confirm_volume_multiple: float = 1.0
    maximum_pullback_volume_multiple: float = 1.0
    min_net_rr: float = 2.0
    max_signal_age_ms: int = 300_000
    max_hold_hours: int = 4
    fee_per_side: float = .0006
    slippage_per_side: float = .0005
    minimum_quote_volume_24h: float = 10_000_000.0


RULES = Rules()
RULES_HASH = hashlib.sha256(json.dumps(asdict(RULES), sort_keys=True, separators=(',',':')).encode()).hexdigest()


class Reject(ValueError):
    pass


def finite(value):
    if isinstance(value, bool):
        raise Reject('INVALID_NUMBER')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise Reject('INVALID_NUMBER') from exc
    if not math.isfinite(result):
        raise Reject('INVALID_NUMBER')
    return result


def closed_bars(raw, now_ms):
    if type(now_ms) is not int or now_ms < 0:
        raise Reject('INVALID_NOW')
    if not isinstance(raw, list) or len(raw) < RULES.history_bars:
        raise Reject('INSUFFICIENT_HISTORY')
    bars = []
    current = now_ms//STEP*STEP
    for i,row in enumerate(raw):
        if not isinstance(row,(list,tuple)) or len(row) < 11:
            raise Reject('MALFORMED_KLINE')
        op,ct = row[0],row[6]
        if type(op) is not int or type(ct) is not int or op < 0 or op%STEP or ct != op+STEP-1:
            raise Reject('KLINE_TIME_CONFLICT')
        if ct >= now_ms:
            if i != len(raw)-1 or op != current:
                raise Reject('UNEXPECTED_UNCLOSED_OR_FUTURE_KLINE')
            continue
        if bars and op != bars[-1]['open_ms']+STEP:
            raise Reject('KLINE_GAP_DUPLICATE_OR_UNSORTED')
        opening,high,low,close,volume = [finite(row[k]) for k in (1,2,3,4,5)]
        if not 0 < low <= min(opening,close) <= max(opening,close) <= high:
            raise Reject('INVALID_OHLC')
        if volume <= 0 or finite(row[7]) <= 0 or not 0 <= finite(row[9]) <= volume:
            raise Reject('INVALID_VOLUME')
        bars.append(dict(open_ms=op,close_ms=ct,opening=opening,high=high,low=low,close=close,volume=volume))
    if len(bars) < RULES.history_bars:
        raise Reject('INSUFFICIENT_CLOSED_HISTORY')
    if bars[-1]['close_ms'] != current-1:
        raise Reject('LATEST_CLOSED_KLINE_MISSING')
    return bars[-RULES.history_bars:]


def ema(values, period):
    result = [values[0]]
    alpha = 2/(period+1)
    for value in values[1:]:
        result.append(alpha*value+(1-alpha)*result[-1])
    return result


def regime(bars):
    # Only full, UTC-aligned 15m groups; partial edges are never observations.
    groups = {}
    for b in bars:
        start = b['open_ms']//RULES.regime_interval_ms*RULES.regime_interval_ms
        groups.setdefault(start,[]).append(b)
    closed = []
    for start,group in sorted(groups.items()):
        if [b['open_ms'] for b in group] == [start,start+STEP,start+2*STEP]:
            closed.append(group[-1])
    if len(closed) < RULES.ema_slow+RULES.regime_slope_bars:
        raise Reject('INSUFFICIENT_REGIME_HISTORY')
    values = [b['close'] for b in closed]
    fast,slow = ema(values,RULES.ema_fast),ema(values,RULES.ema_slow)
    direction = 'NEUTRAL'
    if fast[-1]>slow[-1] and fast[-1]>fast[-2] and slow[-1]>slow[-1-RULES.regime_slope_bars]:
        direction = 'LONG'
    if fast[-1]<slow[-1] and fast[-1]<fast[-2] and slow[-1]<slow[-1-RULES.regime_slope_bars]:
        direction = 'SHORT'
    changes = [abs(b-a) for a,b in zip(values[-22:],values[-21:])]
    efficiency = abs(values[-1]-values[-22])/sum(changes) if sum(changes)>0 else 0.0
    return dict(direction=direction,ema_fast=fast[-1],ema_slow=slow[-1],
                last_closed_15m_ms=closed[-1]['close_ms'],closed_15m_count=len(closed),
                efficiency_observation=efficiency,efficiency_is_entry_gate=False)


def evaluate_setup(raw, *, now_ms, symbol):
    """Return a rule match, never permission to trade or a confidence estimate."""
    evidence = dict(policy='REGIME_PULLBACK_R1',rules_hash=RULES_HASH,
                    profitability='NOT_ESTABLISHED',execution_authorized=False)
    try:
        if not isinstance(symbol,str) or not symbol.endswith('USDT') or not symbol.isalnum():
            raise Reject('INVALID_SYMBOL')
        bars = closed_bars(raw,now_ms)
        context = regime(bars)
        evidence['regime'] = context
        direction = context['direction']
        if direction == 'NEUTRAL':
            raise Reject('NO_DIRECTIONAL_REGIME')
        sign = 1 if direction == 'LONG' else -1
        oriented = [dict(b, qopen=sign*b['opening'],qclose=sign*b['close'],
                         qhigh=b['high'] if sign==1 else -b['low'],
                         qlow=b['low'] if sign==1 else -b['high']) for b in bars]
        last = len(bars)-1
        # The most recent confirmed extremum is the setup; no search over older
        # anchors to find whichever would pass a target or RR threshold.
        pivots = [i for i in range(last-RULES.maximum_pivot_age_bars,last-RULES.minimum_pivot_age_bars+1)
                  if all(oriented[i]['qhigh']>oriented[j]['qhigh']
                         for j in range(i-RULES.pivot_left,i+RULES.pivot_right+1) if j != i)]
        if not pivots:
            raise Reject('NO_CONFIRMED_IMPULSE_EXTREMUM')
        pivot = pivots[-1]
        ranges = [max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close']))
                  for a,b in zip(bars[:pivot-1],bars[1:pivot])]
        atr = sum(ranges[-RULES.atr_bars:])/RULES.atr_bars
        if not atr>0:
            raise Reject('INVALID_ATR')
        base_index = min(range(pivot-RULES.impulse_base_bars,pivot),key=lambda i:oriented[i]['qlow'])
        peak = oriented[pivot]['qhigh']
        impulse = peak-oriented[base_index]['qlow']
        evidence.update(impulse_base_ms=bars[base_index]['open_ms'],impulse_extremum_ms=bars[pivot]['open_ms'],
                        impulse_atr=impulse/atr,atr=atr,direction=direction)
        if impulse < RULES.minimum_impulse_atr*atr:
            raise Reject('IMPULSE_TOO_SMALL')
        pullback = oriented[pivot+1:-1]
        if any(b['qhigh']>=peak for b in pullback):
            raise Reject('IMPULSE_ALREADY_REVISITED')
        floor = min(b['qlow'] for b in pullback)
        # Keep the original decimal prices for this inclusive boundary gate.
        # Reconstructing them from floats can reject an exact 25% retrace or
        # admit a value just outside 60%. The other rule calculations stay intact.
        raw_by_open = {row[0]: row for row in raw}
        high_column,low_column = (2,3) if sign==1 else (3,2)
        decimal_peak = sign*Decimal(str(raw_by_open[bars[pivot]['open_ms']][high_column]))
        decimal_base = sign*Decimal(str(raw_by_open[bars[base_index]['open_ms']][low_column]))
        decimal_floor = min(sign*Decimal(str(raw_by_open[b['open_ms']][low_column])) for b in pullback)
        decimal_impulse = decimal_peak-decimal_base
        decimal_retrace = decimal_peak-decimal_floor
        retrace = float(decimal_retrace/decimal_impulse)
        evidence['retrace_fraction'] = retrace
        # Cross-multiply to avoid rounding the ratio before deciding admission.
        if not Decimal(str(RULES.retrace_min))*decimal_impulse<=decimal_retrace<=Decimal(str(RULES.retrace_max))*decimal_impulse:
            raise Reject('PULLBACK_DEPTH_OUTSIDE_RULE')
        baseline = median(b['volume'] for b in bars[pivot-20:pivot])
        contraction = median(b['volume'] for b in pullback)/baseline
        evidence['pullback_volume_multiple'] = contraction
        if contraction > RULES.maximum_pullback_volume_multiple:
            raise Reject('PULLBACK_VOLUME_NOT_CONTRACTING')
        trigger,previous = oriented[-1],oriented[-2]
        anchors = dict(policy=RULES_HASH,symbol=symbol,direction=direction,
                       base_ms=bars[base_index]['open_ms'],extremum_ms=bars[pivot]['open_ms'])
        if not (trigger['qclose']>previous['qhigh']+RULES.confirmation_atr*atr
                and trigger['qclose']>trigger['qopen'] and trigger['qlow']>=floor):
            confirmation_qclose=previous['qhigh']+RULES.confirmation_atr*atr
            confirmation_gap_atr=(confirmation_qclose-trigger['qclose'])/atr
            watch_anchors=dict(anchors,kind='PULLBACK_CONFIRMATION_WATCH')
            evidence.update(confirmation_gap_atr=confirmation_gap_atr,
                            watch=dict(
                                kind='PULLBACK_CONFIRMATION_WATCH',
                                watch_id=hashlib.sha256(json.dumps(watch_anchors,sort_keys=True).encode()).hexdigest()[:32],
                                symbol=symbol,direction=direction,
                                observation_close_ms=bars[-1]['close_ms'],
                                confirmation_relation='ABOVE' if direction=='LONG' else 'BELOW',
                                confirmation_price=sign*confirmation_qclose,
                                confirmation_gap_atr=confirmation_gap_atr,
                                entry_authorized=False,
                            ))
            raise Reject('NO_PULLBACK_CONFIRMATION')
        if trigger['volume'] < baseline*RULES.minimum_confirm_volume_multiple:
            raise Reject('CONFIRMATION_VOLUME_LOW')
        stop = floor-RULES.stop_buffer_atr*atr
        target = peak-RULES.target_buffer_atr*atr
        risk = trigger['qclose']-stop
        if not RULES.min_stop_atr*atr<=risk<=RULES.max_stop_atr*atr:
            raise Reject('STOP_DISTANCE_OUTSIDE_RULE')
        # Any same-side confirmed obstacle between entry and the setup extremum
        # shortens the target; no imaginary target is extended to manufacture RR.
        obstacles = [oriented[i]['qhigh']-RULES.target_buffer_atr*atr
                     for i in range(2,pivot-1)
                     if all(oriented[i]['qhigh']>oriented[j]['qhigh'] for j in range(i-2,i+3) if j!=i)
                     and oriented[i]['qhigh']-RULES.target_buffer_atr*atr>trigger['qclose']]
        if obstacles:
            target=min(target,min(obstacles))
        if target<=trigger['qclose'] or trigger['qhigh']>=target:
            raise Reject('TARGET_ALREADY_TOUCHED_OR_BEHIND_ENTRY')
        close = bars[-1]['close']
        real_stop,real_target = sign*stop,sign*target
        low,high = close-RULES.entry_buffer_atr*atr,close+RULES.entry_buffer_atr*atr
        if min(real_stop,real_target,low)<=0:
            raise Reject('INVALID_PRICE_LEVELS')
        setup_id = hashlib.sha256(json.dumps(anchors,sort_keys=True).encode()).hexdigest()[:32]
        evidence.update(target_basis='PRIOR_CONFIRMED_EXTREMUM_OR_NEARER_OBSTACLE',
                        target=real_target,stop=real_stop,setup_id=setup_id)
        alert = dict(event_id=setup_id,setup_id=setup_id,symbol=symbol,direction=direction,
                     strategy='REGIME_PULLBACK_R1',strategy_id='REGIME_PULLBACK_R1',
                     strategy_identity=dict(version='3.0.0-r1',rules_hash=RULES_HASH),
                     data_as_of_ms=bars[-1]['close_ms'],expiry_ms=bars[-1]['close_ms']+RULES.max_signal_age_ms,
                     entry_low=low,entry_high=high,reference_close=close,stop=real_stop,
                     targets=[real_target,None,None],fee_per_side=RULES.fee_per_side,
                     slippage_per_side=RULES.slippage_per_side,required_execution_net_rr=RULES.min_net_rr,
                     max_hold_hours=RULES.max_hold_hours,net_rr_to_tp1=None,
                     pattern=dict(path='REGIME_PULLBACK_R1',atr=atr,trigger_close_time=bars[-1]['close_ms'],
                                  trigger_close=close,impulse_base_ms=bars[base_index]['open_ms'],
                                  impulse_extremum_ms=bars[pivot]['open_ms'],retrace_fraction=retrace),
                     provenance='RESEARCH_ONLY_NOT_PROFITABILITY_VALIDATED',execution_authorized=False)
        return alert,'RULE_MATCH',evidence
    except Reject as exc:
        return None,str(exc),evidence

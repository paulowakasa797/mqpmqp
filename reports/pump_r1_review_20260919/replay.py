"""Offline, fixed-clock structure replay. No quote synthesis, orders or notifications."""
import argparse
from collections import Counter
import datetime as dt
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
STEP=300_000


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rules(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def quality(raw):
    if not isinstance(raw,list) or not raw: raise ValueError('EMPTY_DATA')
    for i,row in enumerate(raw):
        if len(row)<11: raise ValueError('MALFORMED_KLINE')
        if type(row[0]) is not int or row[0]%STEP or row[6]!=row[0]+STEP-1:
            raise ValueError('INVALID_TIME_AXIS')
        if i and row[0]!=raw[i-1][0]+STEP: raise ValueError('GAP_DUPLICATE_UNSORTED')
        o,h,l,c,v,q,taker,taker_q=map(float,[row[k] for k in (1,2,3,4,5,7,9,10)])
        if not all(math.isfinite(x) for x in (o,h,l,c,v,q,taker,taker_q)): raise ValueError('NON_FINITE')
        if not 0<l<=min(o,c)<=max(o,c)<=h: raise ValueError('INVALID_OHLC')
        if v<=0 or q<=0 or not 0<=taker<=v or not 0<=taker_q<=q: raise ValueError('INVALID_VOLUME')
    return dict(status='PASS',bars=len(raw),first_open_ms=raw[0][0],last_close_ms=raw[-1][6],
                gaps=0,duplicates=0,nonfinite=0,invalid_ohlc=0)


def context_at(raw,now_ms):
    # Make future rows inaccessible to the strategy; require the latest closed bar.
    cutoff=now_ms//STEP*STEP-1
    return [r for r in raw if r[6]<=cutoff][-180:]


def write_json(path,value):
    with path.open('x',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False);handle.write('\n')


def replay(evidence,baseline_path,candidate_path,output):
    receipt=json.loads((evidence/'receipt.json').read_text(encoding='utf-8'))
    modules={'baseline':load_rules(baseline_path,'replay_baseline'),'candidate':load_rules(candidate_path,'replay_candidate')}
    assert digest(baseline_path)==receipt['sources']['candidate/rules.py']['sha256'], 'BASELINE_DRIFT'
    market={};checks={}
    for symbol in receipt['symbols']:
        path=evidence/(symbol+'_5m.json')
        assert digest(path)==receipt['market'][symbol]['response_sha256'],'DATA_HASH_MISMATCH'
        market[symbol]=json.loads(path.read_bytes());checks[symbol]=quality(market[symbol])
    start,end=[int(dt.datetime.fromisoformat(t).timestamp()*1000) for t in receipt['window_utc']]
    cycles=[json.loads(line) for line in (evidence/'cycles_selected.jsonl').read_text(encoding='utf-8').splitlines()]
    # Independent baseline reproduction at the actual recorded decision clocks.
    ledger_check=Counter(); mismatches=[]
    for cycle in cycles:
        for row in cycle['evaluations']:
            now=row.get('evaluated_server_ms')
            if now is None: ledger_check['unverifiable_missing_clock']+=1;continue
            _,reason,_=modules['baseline'].evaluate_setup(context_at(market[row['symbol']],now),now_ms=now,symbol=row['symbol'])
            expected='RULE_MATCH' if row['stage']=='PUBLIC_PREFLIGHT' else row['reason']
            if reason==expected: ledger_check['matched']+=1
            else:
                ledger_check['mismatched']+=1
                mismatches.append(dict(symbol=row['symbol'],now_ms=now,ledger_reason=expected,replayed_reason=reason))
    output.mkdir(parents=True,exist_ok=False)
    counts={name:Counter() for name in modules}; unique={name:set() for name in modules}
    symbol_counts={s:{n:Counter() for n in modules} for s in market}
    transitions=Counter(); changed=0; observations=0
    decisions=output/'decisions.jsonl'
    with decisions.open('x',encoding='utf-8') as handle:
        for symbol,raw in market.items():
            for now in range(start,end,STEP):
                context=context_at(raw,now)
                input_hash=hashlib.sha256(json.dumps(context,separators=(',',':')).encode()).hexdigest()
                record=dict(symbol=symbol,decision_ms=now,input_cutoff_ms=context[-1][6],input_sha256=input_hash,
                            market_file_sha256=receipt['market'][symbol]['response_sha256'],execution_authorized=False)
                observations+=1
                for name,module in modules.items():
                    alert,reason,ev=module.evaluate_setup(context,now_ms=now,symbol=symbol)
                    counts[name][reason]+=1;symbol_counts[symbol][name][reason]+=1
                    fresh=bool(alert and alert['setup_id'] not in unique[name])
                    if alert: unique[name].add(alert['setup_id'])
                    record[name]=dict(reason=reason,alert=alert,evidence=ev,first_unique_setup=fresh,
                                      rules_hash=module.RULES_HASH)
                a,b=record['baseline']['reason'],record['candidate']['reason']
                if a!=b: transitions[a+' -> '+b]+=1;changed+=1
                handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
    totals=Counter()
    for cycle in cycles: totals.update(cycle.get('reason_counts') or {})
    timestamps=sorted(dt.datetime.fromisoformat(r['created_utc']).timestamp() for r in cycles)
    summary=dict(scope='STRUCTURE_ONLY_NOT_EXECUTABLE_SIGNALS',window_utc=receipt['window_utc'],symbols=receipt['symbols'],
        python=sys.version,data_quality=checks,observations=observations,counts=counts,symbol_counts=symbol_counts,
        unique_structural_setups={n:len(s) for n,s in unique.items()},changed_reason_count=changed,transitions=transitions,
        online_cycles=len(cycles),online_evaluations=sum(r['evaluation_count'] for r in cycles),
        online_selected=sum(r['selected_count'] for r in cycles),online_reasons=totals,
        online_statuses=Counter(r['status'] for r in cycles),duplicate_cycle_timestamps=len(timestamps)-len(set(timestamps)),
        max_cycle_gap_seconds=max((b-a for a,b in zip(timestamps,timestamps[1:])),default=None),
        baseline_ledger_reproduction=dict(counts=ledger_check,mismatches=mismatches),
        source_sha256={n:digest(p) for n,p in [('baseline',baseline_path),('candidate',candidate_path)]},
        decisions_sha256=digest(decisions),strategy_pnl=None,strict_selected=None,
        historical_public_preflight='BLOCKED_MISSING_ORIGINAL_QUOTES_AND_RECEIPTS',
        historical_first_availability='UNKNOWN_DOWNLOADED_LATER',research_gain='INCONCLUSIVE')
    write_json(output/'summary.json',summary)
    print(json.dumps({k:summary[k] for k in ('observations','counts','unique_structural_setups','changed_reason_count','baseline_ledger_reproduction','online_selected')},ensure_ascii=False))


def evaluate(evidence,run,output):
    """Separate step reads sealed decisions before future prices; no strategy call."""
    summary=json.loads((run/'summary.json').read_text(encoding='utf-8'))
    assert digest(run/'decisions.jsonl')==summary['decisions_sha256'],'DECISION_HASH_MISMATCH'
    receipt=json.loads((evidence/'receipt.json').read_text(encoding='utf-8'))
    market={}
    for s in summary['symbols']:
        path=evidence/(s+'_5m.json')
        assert digest(path)==receipt['market'][s]['response_sha256'],'DATA_HASH_MISMATCH'
        market[s]={r[6]+1:float(r[4]) for r in json.loads(path.read_bytes())}
    rows=[]
    for line in (run/'decisions.jsonl').read_text(encoding='utf-8').splitlines():
        record=json.loads(line)
        for name in ('baseline','candidate'):
            prediction=record[name]
            if not prediction['first_unique_setup']:continue
            alert=prediction['alert']; labels={}
            for minutes in (30,60,240):
                future=market[record['symbol']].get(record['decision_ms']+minutes*60000)
                value=None if future is None else (future/alert['reference_close']-1)*(1 if alert['direction']=='LONG' else -1)
                labels[str(minutes)]=dict(status='MATURE' if future is not None else 'INCOMPLETE_FUTURE',directional_close_return=value)
            rows.append(dict(symbol=record['symbol'],decision_ms=record['decision_ms'],policy=name,setup_id=alert['setup_id'],
                             labels=labels,entry_filled=None,trade_pnl=None,decision_file_sha256=summary['decisions_sha256']))
    write_json(output,dict(scope='POST_HOC_DIRECTIONAL_PRICE_LABELS_NOT_TRADE_PNL',records=rows,strategy_pnl=None,research_gain='INCONCLUSIVE'))
    print(json.dumps(dict(evaluated_unique_structures=len(rows),strategy_pnl=None,output=str(output))))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('replay');run.add_argument('--evidence',type=Path,default=HERE/'evidence')
    run.add_argument('--baseline',type=Path,default=HERE/'evidence/baseline/candidate/rules.py')
    run.add_argument('--candidate',type=Path,required=True);run.add_argument('--output',type=Path,required=True)
    labels=sub.add_parser('evaluate');labels.add_argument('--evidence',type=Path,default=HERE/'evidence')
    labels.add_argument('--run',type=Path,required=True);labels.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='replay':replay(args.evidence,args.baseline,args.candidate,args.output)
    else:evaluate(args.evidence,args.run,args.output)


if __name__=='__main__':main()

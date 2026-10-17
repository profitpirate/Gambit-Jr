import asyncio
import copy
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_prearmed_notices import format_notice, format_daily
from memecoin_bot.v12_prearmed_reporting import NoticeOutbox, DailyReporter, daily_summary, day_bounds, UK
from memecoin_bot.v12_prearmed_runtime import Runtime, State

MODEL = json.loads((Path(__file__).resolve().parents[1]/'research/v12-pre-armed-100-trade-frozen-model.json').read_text())


def ns(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=UK).timestamp()*1e9)


@pytest.fixture
def bot(tmp_path):
    (tmp_path/'prearmed-data').mkdir();(tmp_path/'operator-data').mkdir()
    state = State(tmp_path/'prearmed-data/live.sqlite',wallet='wallet',mode='live')
    clock = [ns('2026-09-30T12:00:00')]
    state.conn.execute("UPDATE prearmed_report_meta SET value=? WHERE key='baseline'",
                       (json.dumps({'started_ns':ns('2026-09-30T00:00:00'),'closed':0,'realized_lamports':0}),))
    r = Runtime(model=MODEL,state=state,rpc=None,builder=None,execution=SimpleNamespace(),wallet='wallet',
                metadata_session=None,emit=lambda *a:None,kill_path=tmp_path/'STOP',clock=lambda:clock[0])
    yield r,clock,tmp_path
    state.conn.close()


def buy(r, *, mint='mint', test=False):
    context = {'creator':'creator','reference_price':1,'launch_price':1}
    if test:context['test_operation_id']='test'
    r.state.data['pending']['buy-'+mint]={'payload':{'side':'BUY','mint':mint,'submitted_ns':r.clock()},'context':context,'reason':'ENTRY'}
    r.apply_result('buy-'+mint,{'status':'CONFIRMED','tokens_raw':100,'sol_delta_lamports':-100_000_000,
                              'reference_price':1,'signature':'buyreceipt','confirmed_ns':r.clock()})


def sell(r, amount=100, received=120_000_000, *, mint='mint', identity='sell'):
    r.state.data['pending'][identity]={'payload':{'side':'SELL','mint':mint,'tokens_raw':str(amount)},'context':{},'reason':'MAXIMUM_HOLD'}
    r.apply_result(identity,{'status':'CONFIRMED','tokens_raw':amount,'sol_delta_lamports':received,'signature':'sellreceipt'})


def text(r, identity):
    return r.state.conn.execute('SELECT message FROM prearmed_notice_messages WHERE id=?',('result:'+identity,)).fetchone()[0]


def test_buy_partial_then_loss_reports_actual_net_and_remaining(bot):
    r,clock,folder=bot
    buy(r)
    assert 'pending' in text(r,'buy-mint') and '0.100000 SOL' in text(r,'buy-mint')
    sell(r,30,40_000_000,identity='partial')
    assert '+0.010000 SOL' in text(r,'partial') and '70.0%' in text(r,'partial')
    clock[0]+=2_000_000_000
    sell(r,70,50_000_000)
    value=text(r,'sell')
    assert '🔴 Loss' in value and '-0.010000 SOL (-10.00%)' in value and 'Held 2.0s' in value
    assert r.state.data['realized_lamports']==-10_000_000
    report=daily_summary(folder/'prearmed-data/live.sqlite',date(2026,9,30))
    assert report['closed']==1 and report['losses']==1 and report['net_pnl_lamports']==-10_000_000
    assert len(format_daily(report,now_ns=clock[0]))<1000


@pytest.mark.parametrize('received,outcome',[ (120_000_000,'WIN'),(100_000_000,'BREAKEVEN'),(80_000_000,'LOSS')])
def test_outcome_and_percentage_include_entry_cost(bot,received,outcome):
    r,_,_=bot;buy(r);sell(r,received=received)
    expected = {'WIN': '✅ Win', 'BREAKEVEN': '⚪ Break-even', 'LOSS': '🔴 Loss'}[outcome]
    assert expected in text(r,'sell')
    assert ('+20.00%' if outcome=='WIN' else '+0.00%' if outcome=='BREAKEVEN' else '-20.00%') in text(r,'sell')


def test_failed_buys_and_sells_are_not_double_counted(bot):
    r,_,folder=bot;buy(r)
    for side,identity in [('BUY','failed-buy'),('SELL','failed-sell')]:
        r.state.data['pending'][identity]={'payload':{'side':side,'mint':'mint'},'context':{},'reason':'ENTRY'}
        r.apply_result(identity,{'status':'FAILED','reason':'CHAIN_REJECTED','fee_lamports':1_000_000})
    sell(r,received=120_000_000)
    report=daily_summary(folder/'prearmed-data/live.sqlite',date(2026,9,30))
    assert report['closed_pnl_lamports']==19_000_000
    assert report['failed_buy_fees_lamports']==1_000_000 and report['net_pnl_lamports']==18_000_000
    assert '0.001000 SOL' in text(r,'failed-buy')


def test_test_trades_and_manual_wallet_changes_excluded(bot):
    r,_,folder=bot;buy(r,test=True);sell(r,received=90_000_000)
    # The report uses confirmed bot fills, never native-balance changes.
    r.balance=400_000_000
    report=daily_summary(folder/'prearmed-data/live.sqlite',date(2026,9,30))
    assert report['closed']==0 and report['net_pnl_lamports']==0
    assert report['test_closed']==1 and report['test_pnl_lamports']==-10_000_000
    assert text(r,'sell').startswith('TEST Gambit')


def test_duplicate_and_unknown_outcomes_do_not_create_second_report(bot):
    r,_,_=bot;buy(r)
    r.apply_result('buy-mint',{'status':'CONFIRMED'})
    r.state.data['pending']['unknown']={'payload':{'side':'SELL','mint':'mint'},'context':{}}
    r.apply_result('unknown',{'status':'UNKNOWN'})
    assert r.state.conn.execute('SELECT COUNT(*) FROM prearmed_trade_reports').fetchone()[0]==1
    assert r.state.reports.outbox.pending()==2


def test_reporting_storage_failure_rolls_back_fill_state_for_existing_receipt_recovery(bot,monkeypatch):
    r,_,_=bot;buy(r)
    before=copy.deepcopy(r.state.data)
    original=r.state.reports.record_result
    def broken(*a):
        original(*a)
        raise OSError('disk full')
    monkeypatch.setattr(r.state.reports,'record_result',broken)
    with pytest.raises(OSError):sell(r)
    assert r.state.data['positions']==before['positions'] and r.state.data['closed']==0
    assert r.state.conn.execute('SELECT COUNT(*) FROM prearmed_trade_reports').fetchone()[0]==1
    assert r.state.conn.execute("SELECT COUNT(*) FROM prearmed_notice_messages WHERE id='result:sell'").fetchone()[0]==0
    monkeypatch.setattr(r.state.reports,'record_result',original)
    r.apply_result('sell',{'status':'CONFIRMED','tokens_raw':100,'sol_delta_lamports':120_000_000,'signature':'existing-receipt'})
    assert r.state.data['closed']==1


class Sinks:
    def __init__(self):self.discord=[];self.phone=[];self.fail_phone=False
    async def send_discord(self,message):self.discord.append(message)
    async def send_pushover(self,message):
        if self.fail_phone:raise RuntimeError('never persist provider secrets')
        self.phone.append(message)


def test_restart_retries_only_failed_channel_with_backoff(tmp_path):
    db=sqlite3.connect(tmp_path/'outbox.sqlite',isolation_level=None);out=NoticeOutbox(db)
    out.enqueue('trade','message',1);out.enqueue('trade','message',1)
    sinks=Sinks();sinks.fail_phone=True
    assert asyncio.run(out.flush(sinks,now_ns=1))==['phone']
    assert sinks.discord==['message'] and not sinks.phone
    assert db.execute("SELECT error_type FROM prearmed_notice_delivery WHERE channel='phone'").fetchone()[0]=='RuntimeError'
    db.close();db=sqlite3.connect(tmp_path/'outbox.sqlite',isolation_level=None);out=NoticeOutbox(db)
    sinks.fail_phone=False
    asyncio.run(out.flush(sinks,now_ns=2));assert not sinks.phone
    asyncio.run(out.flush(sinks,now_ns=3_000_000_000))
    assert sinks.discord==['message'] and sinks.phone==['message'] and out.pending()==0
    db.close()


def test_calendar_close_day_not_entry_day_and_dst(bot):
    r,clock,folder=bot;clock[0]=ns('2026-09-30T23:59:59');buy(r)
    clock[0]=ns('2026-10-01T00:00:01');sell(r)
    assert daily_summary(folder/'prearmed-data/live.sqlite',date(2026,9,30))['closed']==0
    assert daily_summary(folder/'prearmed-data/live.sqlite',date(2026,10,1))['closed']==1
    for day,hours in [(date(2026,3,29),23),(date(2026,10,25),25)]:
        start,end=day_bounds(day);assert end-start==hours*3600*10**9


def test_daily_zero_trade_day_delivered_once_even_after_restart_and_while_bot_stopped(bot):
    r,clock,folder=bot;sinks=Sinks();reporter=DailyReporter(folder)
    asyncio.run(reporter.tick(sinks,now_ns=clock[0]))
    asyncio.run(reporter.tick(sinks,now_ns=ns('2026-10-01T00:04:59')));assert not sinks.discord
    asyncio.run(reporter.tick(sinks,now_ns=ns('2026-10-01T00:05:00')))
    assert len(sinks.discord)==len(sinks.phone)==1 and 'No bot trades closed today' in sinks.phone[0]
    assert '📊 Gambit daily P/L' in sinks.phone[0] and '⚪ Net for the day: +0.000000 SOL' in sinks.phone[0]
    reporter.conn.close();reporter=DailyReporter(folder)
    asyncio.run(reporter.tick(sinks,now_ns=ns('2026-10-01T00:07:00')))
    assert len(sinks.discord)==1
    reporter.conn.close()


def test_daily_catches_up_days_missed_by_operator_and_marks_partial_first_day(bot):
    r,clock,folder=bot
    r.state.conn.execute("UPDATE prearmed_report_meta SET value=? WHERE key='baseline'",
                       (json.dumps({'started_ns':clock[0],'closed':0,'realized_lamports':0}),))
    sinks=Sinks();reporter=DailyReporter(folder)
    asyncio.run(reporter.tick(sinks,now_ns=clock[0]))
    asyncio.run(reporter.tick(sinks,now_ns=ns('2026-10-03T08:00:00')))
    assert len(sinks.discord)==3 and 'Partial first day' in sinks.discord[0]
    reporter.conn.close()


def test_mismatched_accounting_never_publishes_false_daily_profit(bot):
    r,_,folder=bot;r.state.data['realized_lamports']=1000;r.state.save()
    with pytest.raises(ValueError,match='DO_NOT_RECONCILE'):
        daily_summary(folder/'prearmed-data/live.sqlite',date(2026,9,30))


def test_message_length_real_addresses_and_unknown_pnl():
    payload={'mint':'A'*44,'side':'SELL','signature':'B'*88,'sol_delta_lamports':90_000_000,
             'entry_debit_lamports':100_000_000,'trade_pnl_lamports':-10_000_000,'position_closed':True,
             'all_time_pnl_lamports':-120_616_909,'exit_reason':'MAXIMUM_HOLD','hold_ms':2001}
    message,_=format_notice('FILL_CONFIRMED',payload)
    assert len(message)<1000 and 'solscan.io/tx/' in message and '-10.00%' in message
    message,priority=format_notice('EXECUTION_OUTCOME_UNKNOWN',{'reason':'https://secret-provider/?token=SECRET'})
    assert 'SECRET' not in message and 'may have gone through' in message and 'Do not retry' in message and priority==1


def test_failed_channel_keeps_trade_order_without_blocking_other_channel(tmp_path):
    db=sqlite3.connect(tmp_path/'queue.sqlite',isolation_level=None);out=NoticeOutbox(db)
    sinks=Sinks();sinks.fail_phone=True
    out.enqueue('buy','BUY',1);out.enqueue('sell','SELL',2)
    asyncio.run(out.flush(sinks,now_ns=10))
    assert sinks.discord==['BUY','SELL'] and not sinks.phone
    sinks.fail_phone=False
    asyncio.run(out.flush(sinks,now_ns=11));assert not sinks.phone
    asyncio.run(out.flush(sinks,now_ns=3_000_000_000))
    assert sinks.phone==['BUY','SELL'] and sinks.discord==['BUY','SELL']
    db.close()


def test_queued_daily_delivery_survives_new_report_accounting_error(bot):
    r,clock,folder=bot;reporter=DailyReporter(folder);sinks=Sinks()
    asyncio.run(reporter.tick(sinks,now_ns=clock[0]))
    sinks.fail_phone=True
    asyncio.run(reporter.tick(sinks,now_ns=ns('2026-10-01T00:05:00')))
    assert len(sinks.discord)==1 and reporter.outbox.pending()==1
    sinks.fail_phone=False
    r.state.data['closed']=1;r.state.save()
    with pytest.raises(ValueError,match='DO_NOT_RECONCILE'):
        asyncio.run(reporter.tick(sinks,now_ns=ns('2026-10-02T00:05:00')))
    assert len(sinks.phone)==1 and len(sinks.discord)==1 and reporter.outbox.pending()==0
    reporter.conn.close()

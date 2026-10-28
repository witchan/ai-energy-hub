"""Isolated regression coverage: durable checkout/read/worker claims and 2FA edges."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
from email.message import Message
import hashlib
import importlib.util
import io
import itertools
import json
import multiprocessing
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import server

DETAIL = {'id':9,'name':'fixture','amounts':[10,20], 'display_buy_rate':'1.025', 'service_fee_rmb':0,'brand_levels':{'level_2':{}}}
PLANS = {'code':0,'data':{'plans':[{'id':9,'name':'fixture','enabled':True,'price_rmb':'10.25','provider_code':'chatgpt'}]}}
KEY = 'a' * 64
BODY = {'product_id':9,'amount':10}


def process_probe(source, dbfile, kind, barrier, entered, release, output):
    spec=importlib.util.spec_from_file_location('isolated_shop',source)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.DB_FILE=dbfile
    m.get_product_detail=lambda _:DETAIL
    m.payment_qr=lambda _: 'fixture-qr'
    def external(value):
        output.put(('call',kind)); entered.set()
        if not release.wait(10): raise TimeoutError('fixture release')
        if kind=='checkout': return {'code':0,'data':{'id':501,'pay_data':{'qr_code':'fixture'}}}
        return {'code':0,'data':{'id':value,'status':0,'amount':11.25}}
    m.create_recharge=external; m.get_recharge_status=external
    with patch.object(m.urllib.request,'urlopen',side_effect=AssertionError('network blocked')):
        barrier.wait(10)
        try:
            if kind=='checkout': m.create_local_order(BODY,request_key=KEY)
            elif kind=='worker': m.maintenance_tick()
            else: m.fulfill_order(1)
            output.put(('done','ok'))
        except m.CheckoutConflictError:
            output.put(('done','pending'))
        except Exception as exc:
            output.put(('error',type(exc).__name__))


class ConcurrencyFixTest(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.directory=self.stack.enter_context(tempfile.TemporaryDirectory())
        for key,value in {'DB_FILE':str(Path(self.directory)/'orders.db'),'ORDER_SECRETS':{},'RATE_LIMITER':server.RateLimiter(), 'TRUST_PROXY':False,'PROFIT':3,'AI_PROFIT':3,'WORKER_SLOTS':threading.BoundedSemaphore(5)}.items():
            self.stack.enter_context(patch.object(server,key,value))
        self.stack.enter_context(patch.object(server.urllib.request,'urlopen',side_effect=AssertionError('network blocked')))
        self.stack.enter_context(patch.object(server,'get_product_detail',return_value=DETAIL))
        self.stack.enter_context(patch.object(server,'get_ai_plans',return_value=PLANS))
        self.stack.enter_context(patch.object(server,'payment_qr',return_value='fixture-qr'))
        ids=itertools.count(501)
        self.recharge=self.stack.enter_context(patch.object(server,'create_recharge',side_effect=lambda _: {'code':0,'data':{'id':next(ids),'pay_data':{'qr_code':'fixture'}}}))
        server.init_db()

    def seed(self,oid=1,**fields):
        row=dict(id=oid,order_no='fixture-'+str(oid),order_type='gift_card',recharge_id=100+oid,upstream_order_id=0,product_id=9,amount=10,quantity=1,cost_price=10.25,sell_price=11.25,status=0,status_text='waiting',created_at=datetime.now().isoformat(),updated_at=datetime.now().isoformat())
        row.update(fields)
        with server.database() as db:
            db.execute('INSERT INTO orders('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))

    def twofa(self,**fields):
        values=dict(fulfillment_state='created',payment_verified=1,upstream_order_id=7001,status=2,credential_state='submitted',need_2fa=1)
        values.update(fields); self.seed(**values)

    def test_checkout_replays_original_result_and_token_after_price_change(self):
        first=server.create_local_order(BODY,request_key=KEY)
        server.init_db()
        with patch.object(server,'get_product_detail',side_effect=AssertionError('replay must not quote')), patch.object(server,'PROFIT',55):
            second=server.create_local_order(BODY,request_key=KEY)
        self.assertEqual(first,second); self.recharge.assert_called_once()
        self.assertTrue(server.order_access_allowed(server.read_order(first['id']),second['access_token']))

    def test_checkout_conflicts_on_changed_product_amount_or_business(self):
        server.create_local_order(BODY,request_key=KEY)
        for body,ai in ((dict(BODY,amount=20),False),(dict(BODY,product_id=10),False),({'plan_id':9},True)):
            with self.assertRaises(server.CheckoutConflictError): server.create_local_order(body,ai,KEY)
        self.recharge.assert_called_once()

    def test_checkout_different_intents_and_customers_remain_independent(self):
        first=server.create_local_order(BODY,request_key=KEY)
        second=server.create_local_order(BODY,request_key='b'*64)
        self.assertNotEqual(first['id'],second['id']); self.assertNotEqual(first['access_token'],second['access_token'])
        self.assertEqual(self.recharge.call_count,2)

    def test_checkout_concurrent_requests_only_one_external_write(self):
        entered,release=threading.Event(),threading.Event()
        def slow(_):
            entered.set(); self.assertTrue(release.wait(5)); return {'code':0,'data':{'id':501,'pay_data':{'qr_code':'fixture'}}}
        self.recharge.side_effect=slow
        with ThreadPoolExecutor(12) as pool:
            first=pool.submit(server.create_local_order,BODY,False,KEY)
            try:
                self.assertTrue(entered.wait(3))
                def retry(_):
                    with self.assertRaises(server.CheckoutConflictError): server.create_local_order(BODY,request_key=KEY)
                list(pool.map(retry,range(11)))
            finally: release.set()
            original=first.result(5)
        self.assertEqual(server.create_local_order(BODY,request_key=KEY),original)
        self.recharge.assert_called_once()

    def test_checkout_uncertain_write_never_retries_even_after_restart(self):
        self.recharge.side_effect=TimeoutError('fixture')
        with self.assertRaises(TimeoutError): server.create_local_order(BODY,request_key=KEY)
        server.init_db()
        with self.assertRaises(server.CheckoutConflictError): server.create_local_order(BODY,request_key=KEY)
        self.recharge.assert_called_once()
        with server.database() as db: self.assertEqual(db.execute('SELECT state FROM checkout_requests').fetchone()[0],'review')

    def test_checkout_orphan_claim_is_not_reissued(self):
        h,f,_=server.checkout_identity(BODY,False,KEY)
        with server.database() as db: db.execute("INSERT INTO checkout_requests VALUES(?,?,'creating',NULL,0)",(h,f))
        with self.assertRaises(server.CheckoutConflictError): server.create_local_order(BODY,request_key=KEY)
        self.recharge.assert_not_called()

    def test_checkout_preflight_error_does_not_consume_key(self):
        with patch.object(server,'get_product_detail',return_value=None):
            with self.assertRaises(ValueError): server.create_local_order(BODY,request_key=KEY)
        self.assertTrue(server.create_local_order(BODY,request_key=KEY)['id'])
        self.recharge.assert_called_once()

    def test_checkout_response_loss_does_not_replace_credentials_or_store_key(self):
        first=server.create_local_order(dict(BODY,account='a@example.invalid',password='fixture-pass'),request_key=KEY)
        replay=server.create_local_order(dict(BODY,account='b@example.invalid',password='different-pass'),request_key=KEY)
        self.assertEqual(first,replay)
        self.assertEqual(server.ORDER_SECRETS[first['id']]['account'],'a@example.invalid')
        with server.database() as db:
            dump='\n'.join(db.iterdump())
        for secret in (KEY,first['access_token'],'fixture-pass','different-pass','a@example.invalid'):
            self.assertNotIn(secret,dump)

    def test_checkout_paid_replay_flags_paid_without_new_recharge(self):
        result=server.create_local_order({'plan_id':9},True,KEY)
        with server.database() as db: db.execute('UPDATE orders SET payment_verified=1 WHERE id=?',(result['id'],))
        self.assertTrue(server.create_local_order({'plan_id':9},True,KEY)['paid'])
        self.recharge.assert_called_once()

    def test_http_requires_valid_idempotency_key_before_external_calls(self):
        for key in (None,'short','g'*64):
            h=object.__new__(server.Handler); h.path='/api/create-order'; h.client_address=('127.0.0.1',1)
            raw=json.dumps(BODY).encode(); h.rfile=io.BytesIO(raw); h.headers=Message()
            h.headers['Content-Type']='application/json'; h.headers['Content-Length']=str(len(raw))
            if key: h.headers['Idempotency-Key']=key
            answers=[]; h.send_json=lambda data,code=200:answers.append((code,data))
            h.do_POST(); self.assertEqual(answers[-1][0],400)
        self.recharge.assert_not_called()

    def test_payment_check_coalesces_and_releases_after_exception(self):
        self.seed()
        with patch.object(server,'get_recharge_status',side_effect=TimeoutError('fixture')):
            with self.assertRaises(TimeoutError): server.fulfill_order(1)
        self.assertEqual(server.read_order(1)['payment_check_until'],0)
        with patch.object(server,'get_recharge_status',return_value={'code':0,'data':{'id':101,'amount':11.25,'status':0}}) as get:
            server.fulfill_order(1)
        get.assert_called_once()

    def test_payment_live_and_expired_leases(self):
        self.seed(payment_check_id='other',payment_check_until=time.time()+180)
        with patch.object(server,'get_recharge_status',return_value={'code':1}) as get:
            server.fulfill_order(1); get.assert_not_called()
            with server.database() as db: db.execute('UPDATE orders SET payment_check_until=0')
            server.fulfill_order(1); get.assert_called_once()

    def test_payment_late_result_does_not_complete_newer_lease(self):
        self.seed()
        def response(_):
            with server.database() as db: db.execute("UPDATE orders SET payment_check_id='new-owner',payment_check_until=?",(time.time()+180,))
            return {'code':0,'data':{'id':101,'status':1,'amount':11.25}}
        with patch.object(server,'get_recharge_status',side_effect=response), patch.object(server,'create_order') as create:
            server.fulfill_order(1)
        self.assertFalse(server.read_order(1)['payment_verified']); create.assert_not_called()
        self.assertEqual(server.read_order(1)['payment_check_id'],'new-owner')

    def run_process_case(self,kind,n):
        ctx=multiprocessing.get_context('spawn'); barrier=ctx.Barrier(n); entered=ctx.Event(); release=ctx.Event(); output=ctx.Queue()
        children=[ctx.Process(target=process_probe,args=(str(Path(server.__file__)),server.DB_FILE,kind,barrier,entered,release,output)) for _ in range(n)]
        for child in children: child.start()
        messages=[]
        try:
            self.assertTrue(entered.wait(10))
            expected_calls=5 if kind=='worker' else 1
            if kind=='worker':
                while sum(a=='call' for a,b in messages)<5: messages.append(output.get(timeout=8))
                time.sleep(.2)
            else:
                while sum(a=='done' for a,b in messages)<n-1: messages.append(output.get(timeout=8))
        finally:
            release.set()
            for child in children:
                child.join(12)
                if child.is_alive(): child.terminate(); child.join()
                self.assertEqual(child.exitcode,0)
        while True:
            try: messages.append(output.get(timeout=.1))
            except queue.Empty: break
        self.assertFalse([x for x in messages if x[0]=='error'],messages)
        self.assertEqual(sum(x[0]=='call' for x in messages),expected_calls,messages)
        output.close(); output.join_thread()

    def test_checkout_deduplicates_across_four_processes(self):
        self.run_process_case('checkout',4)
        with server.database() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],1)

    def test_payment_check_coalesces_across_four_processes(self):
        self.seed(); self.run_process_case('payment',4)

    def test_workers_share_global_five_active_leases_across_processes(self):
        for oid in range(1,16): self.seed(oid)
        self.run_process_case('worker',2)

    def test_worker_ticks_return_and_other_orders_progress_during_slow_job(self):
        for oid in range(1,9): self.seed(oid)
        entered,release,later=threading.Event(),threading.Event(),threading.Event()
        calls=[]
        def slow(rid):
            calls.append(rid)
            if rid==101: entered.set(); release.wait(5)
            else: later.set()
            return {'code':1}
        with patch.object(server,'get_recharge_status',side_effect=slow), ThreadPoolExecutor(5) as executor:
            try:
                start=time.monotonic(); server.maintenance_tick(executor)
                self.assertLess(time.monotonic()-start,1)
                self.assertTrue(entered.wait(2)); self.assertTrue(later.wait(2))
                server.maintenance_tick(executor)
            finally: release.set()
        self.assertEqual(calls.count(101),1)
        with server.database() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM orders WHERE worker_lease_until>0').fetchone()[0],0)

    def test_worker_live_lease_skipped_expired_lease_recovered(self):
        self.seed(worker_lease_id='other',worker_lease_until=time.time()+600)
        with patch.object(server,'get_recharge_status',return_value={'code':1}) as get:
            server.maintenance_tick(); get.assert_not_called()
            with server.database() as db: db.execute('UPDATE orders SET worker_lease_until=0')
            server.maintenance_tick(); get.assert_called_once()

    def test_worker_failed_submit_releases_slot_and_claim(self):
        self.seed()
        class Broken:
            def submit(self,*args): raise RuntimeError('executor closed')
        with self.assertRaises(RuntimeError): server.maintenance_tick(Broken())
        self.assertEqual(server.read_order(1)['worker_lease_until'],0)
        acquired=[server.WORKER_SLOTS.acquire(False) for _ in range(5)]
        for _ in acquired: server.WORKER_SLOTS.release()
        self.assertTrue(all(acquired))

    def test_twofa_success_stale_true_and_missing_flag_do_not_reopen(self):
        self.twofa()
        with patch.object(server,'submit_2fa',return_value={'code':0}) as submit:
            server.deliver_2fa(server.read_order(1),'123456')
            server.init_db()
            for fields in ({'allow_submit_2fa':True},{},{'allow_submit_2fa':True}):
                with patch.object(server,'get_order_status',return_value={'code':0,'data':dict(status=2,**fields)}): server.refresh_order(server.read_order(1))
                self.assertFalse(server.order_flags(server.read_order(1))['need_2fa'])
                with self.assertRaises(ValueError): server.deliver_2fa(server.read_order(1),'123456')
        submit.assert_called_once()

    def test_twofa_false_then_true_also_cannot_prove_new_challenge(self):
        self.twofa()
        with patch.object(server,'submit_2fa',return_value={'code':0}) as submit:
            server.deliver_2fa(server.read_order(1),'123456')
            for value in (False,True):
                with patch.object(server,'get_order_status',return_value={'code':0,'data':{'status':2,'allow_submit_2fa':value}}): server.refresh_order(server.read_order(1))
            self.assertFalse(server.order_flags(server.read_order(1))['need_2fa'])
            with self.assertRaises(ValueError): server.deliver_2fa(server.read_order(1),'654321')
        submit.assert_called_once()

    def test_twofa_confirmed_rejection_allows_retry_but_review_stays_closed(self):
        self.twofa()
        with patch.object(server,'submit_2fa',return_value={'code':1}):
            with self.assertRaises(ValueError): server.deliver_2fa(server.read_order(1),'123456')
        self.assertEqual(server.read_order(1)['twofa_state'],'idle')
        with patch.object(server,'submit_2fa',return_value={'code':502,'ambiguous':True}):
            with self.assertRaises(ValueError): server.deliver_2fa(server.read_order(1),'123456')
        for value in (False,True):
            with patch.object(server,'get_order_status',return_value={'code':0,'data':{'status':2,'allow_submit_2fa':value}}): server.refresh_order(server.read_order(1))
        self.assertEqual(server.read_order(1)['twofa_state'],'review')
        self.assertFalse(server.order_flags(server.read_order(1))['need_2fa'])

    def test_twofa_late_response_does_not_overwrite_terminal_or_review(self):
        self.twofa()
        def late(*_):
            with server.database() as db: db.execute("UPDATE orders SET status=4,twofa_state='review',need_2fa=0,status_text='terminal'")
            return {'code':0}
        with patch.object(server,'submit_2fa',side_effect=late): server.deliver_2fa(server.read_order(1),'123456')
        row=server.read_order(1)
        self.assertEqual((row['status'],row['twofa_state'],row['need_2fa'],row['status_text']),(4,'review',0,'terminal'))

    def test_twofa_attempt_ownership_and_stale_poll_snapshot(self):
        self.twofa(twofa_state='submitting',twofa_attempt_id='new')
        self.assertEqual(server.finish_twofa_attempt(1,'old','idle',True),0)
        old=server.read_order(1)
        self.assertEqual(server.finish_twofa_attempt(1,'new','submitted',False),1)
        with patch.object(server,'get_order_status',return_value={'code':0,'data':{'status':2,'allow_submit_2fa':False}}): server.refresh_order(old)
        self.assertEqual(server.read_order(1)['twofa_state'],'submitted')

    def test_migration_is_idempotent_and_internal_fields_are_hidden(self):
        self.seed(); before=server.read_order(1)
        server.init_db(); server.init_db(); self.assertEqual(server.read_order(1),before)
        public=server.public_order(before)
        for key in ('payment_check_id','payment_check_until','worker_lease_id','worker_lease_until','twofa_attempt_id'):
            self.assertNotIn(key,public)

if __name__=='__main__': unittest.main()

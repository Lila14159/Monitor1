import copy
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crypto_monitor.cli import encoded, write_json, prepare, deliver, hosts, market_event
from crypto_monitor.delivery import DeliveryError, post
from crypto_monitor.http import Client, FetchError
from crypto_monitor.scheduler import digest_slot
from crypto_monitor.formatting import message

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)
WEBHOOK = 'https://discord.com/api/webhooks/123456789012345678/' + 'a' * 60


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def open(self, req, timeout):
        self.requests.append(req)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return Response(reply)


def snapshot():
    return {'as_of': NOW.isoformat(), 'coins': [{'id':'bitcoin','symbol':'btc','current_price':100,
            'last_updated':NOW.isoformat()}], 'exchanges':[], 'coverage':{}, 'observations':[]}


def state():
    return {'version':1,'initialized':True,'engine':{'initialized':True},'digest_baseline':{'bitcoin':{'price':100}}}


def event(key='event', alert=False):
    return {'id':key,'category':'regulatory','title':'A material announcement','summary':'Source details',
            'assets':['BTC'],'impact':'Potential liquidity effect; direction uncertain.',
            'sources':[{'name':'Regulator','url':'https://www.sec.gov/news/one'}],
            'published_at':NOW.isoformat(),'severity':'major','alert':alert}


class SchedulingTests(unittest.TestCase):
    def test_taipei_slots_delay_and_quiet_slot_dedup(self):
        slot = digest_slot(NOW, {})
        self.assertEqual(slot, '2026-10-21T09:00@Asia/Taipei')
        self.assertIsNone(digest_slot(NOW, {slot:NOW.isoformat()}))
        self.assertEqual(digest_slot(NOW + timedelta(hours=2), {}), slot)
        self.assertIsNone(digest_slot(NOW + timedelta(hours=3,seconds=1), {}))
        self.assertIsNone(digest_slot(NOW - timedelta(seconds=1), {}))

    def test_timezone_dst_no_manual_offset(self):
        for utc_time in [datetime(2026,1,20,14,tzinfo=UTC),datetime(2026,7,20,13,tzinfo=UTC)]:
            self.assertIn('T09:00@America/New_York', digest_slot(utc_time, {}, 'America/New_York'))
        # A folded hour is assigned one civil slot, never twice.
        first = datetime(2026,11,1,5,30,tzinfo=UTC)
        slot = digest_slot(first, {}, 'America/New_York', [1])
        self.assertIsNone(digest_slot(first+timedelta(hours=1),{slot:first.isoformat()},'America/New_York',[1]))


class HttpTests(unittest.TestCase):
    def test_destination_credentials_and_redirects(self):
        client = Client({'api.coingecko.com'}, resolve=False, opener=Opener([]))
        for url in ['http://api.coingecko.com/a','https://127.0.0.1','https://api.coingecko.com.evil.test/a',
                    'https://secret@api.coingecko.com/a','https://api.coingecko.com:444/a']:
            with self.assertRaises(FetchError): client.get_json(url)
        with self.assertRaises(FetchError):
            client.get_json('https://api.coingecko.com/a', {'CG-API-KEY':'secret'})
        client = Client({'api.coingecko.com'},resolve=False,opener=Opener([
            urllib.error.HTTPError('https://secret',302,'redirect',{},None)]))
        with self.assertRaisesRegex(FetchError,'302'): client.get_json('https://api.coingecko.com/a')

    def test_retry_bounds_redaction_size_and_budget(self):
        error = urllib.error.HTTPError('https://secret',429,'secret',{'Retry-After':'1'},None)
        opener = Opener([error,b'{"ok":true}'])
        sleeps = []
        client = Client({'data.test'},resolve=False,opener=opener,sleep=sleeps.append,max_requests=2)
        self.assertEqual(client.get_json('https://data.test'),{'ok':True})
        self.assertEqual(sleeps,[1])
        with self.assertRaisesRegex(FetchError,'budget'): client.get_json('https://data.test')
        client = Client({'data.test'},resolve=False,opener=Opener([b'a'*20]),max_bytes=10)
        with self.assertRaisesRegex(FetchError,'size'): client.get_json('https://data.test')
        client = Client({'data.test'},resolve=False,opener=Opener([urllib.error.URLError('SECRET'),urllib.error.URLError('SECRET')]),sleep=lambda _:None)
        with self.assertRaises(FetchError) as err: client.get_json('https://data.test')
        self.assertNotIn('SECRET',str(err.exception))


class DeliveryTests(unittest.TestCase):
    def test_wait_ack_disable_mentions_and_avoid_retry(self):
        payload = message(event(),'Digest',NOW.isoformat())
        opener = Opener([b'{"id":"123456789012345678"}'])
        self.assertEqual(post(WEBHOOK,payload,opener=opener,resolve=False),'123456789012345678')
        req = opener.requests[0]
        self.assertEqual(req.method,'POST')
        self.assertTrue(req.full_url.endswith('?wait=true'))
        self.assertEqual(json.loads(req.data)['allowed_mentions'],{'parse':[]})
        opener = Opener([urllib.error.URLError('TOP_SECRET')])
        with self.assertRaises(DeliveryError) as err: post(WEBHOOK,payload,opener=opener,resolve=False)
        self.assertEqual(len(opener.requests),1)
        self.assertNotIn('TOP_SECRET',str(err.exception))
        with self.assertRaises(DeliveryError): post('https://discord.com.evil.test/api/webhooks/123/a',payload)

    def test_bounded_message_and_sanitized_source_text(self):
        item=event();item['title']='@everyone <script>do bad things</script>'+'A'*3000
        payload=message(item,'Alert',NOW.isoformat())
        self.assertLessEqual(len(payload['content']),1900)
        self.assertNotIn('@everyone',payload['content'])
        self.assertIn('https://www.sec.gov/news/one',payload['content'])

    def test_durable_intent_no_replay_after_ambiguous_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path=Path(folder)/'state.json'; plan_path=Path(folder)/'plan.json'
            original={'version':1,'initialized':True,'outbox':{'a':{'status':'pending','created_at':NOW.isoformat(),'event_ids':['x'],
                       'payload':message(event(),'Alert',NOW.isoformat())}}}
            write_json(state_path,original)
            import hashlib
            write_json(plan_path,{'created_at':NOW.isoformat(),'dry_run':False,'message_ids':['a'],
                                  'state_sha256':hashlib.sha256(encoded(original)).hexdigest()})
            calls=[]
            def fail(*args):
                calls.append(args)
                self.assertEqual(json.loads(state_path.read_text())['outbox']['a']['status'],'attempted')
                raise DeliveryError('uncertain')
            with self.assertRaises(DeliveryError): deliver(state_path,plan_path,send=True,webhook=WEBHOOK,poster=fail,now=NOW)
            self.assertEqual(json.loads(state_path.read_text())['outbox']['a']['status'],'uncertain')
            with self.assertRaisesRegex(ValueError,'replay'): deliver(state_path,plan_path,send=True,webhook=WEBHOOK,poster=fail,now=NOW)
            self.assertEqual(len(calls),1)


class PlanningTests(unittest.TestCase):
    def evaluate(self,snapshot,state,config,now):
        return copy.deepcopy(state), copy.deepcopy(self.events)

    def setUp(self):
        self.events=[]
        self.patcher=patch('crypto_monitor.engine.evaluate',side_effect=self.evaluate)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_no_change_does_not_send_digest(self):
        updated,ids,summary=prepare(snapshot(),state(),{},NOW)
        self.assertEqual(ids,[])
        self.assertIn('2026-10-21T09:00@Asia/Taipei',updated['digest_slots'])

    def test_alert_assigned_once_and_excluded_from_digest(self):
        self.events=[event('one',True)]
        updated,ids,_=prepare(snapshot(),state(),{},NOW)
        self.assertEqual(len(ids),1)
        updated2,ids2,_=prepare(snapshot(),updated,{},NOW+timedelta(minutes=17))
        self.assertEqual(ids2,[])
        self.assertEqual(updated2['pending_events'],{})

    def test_message_caps_hold_overflow(self):
        self.events=[event(str(n),True) for n in range(7)]
        updated,ids,_=prepare(snapshot(),state(),{'safety':{'max_messages_per_run':2}},NOW)
        self.assertEqual(len(ids),2)
        self.assertEqual(len(updated['pending_events']),5)
        self.events=[]
        _,ids,_=prepare(snapshot(),updated,{'safety':{'max_messages_per_day':2}},NOW+timedelta(hours=1))
        self.assertEqual(ids,[])

    def test_market_moves_are_digest_only_and_stale_prices_rejected(self):
        snap=snapshot();snap['coins'][0]['current_price']=110
        item=market_event(snap,{'bitcoin':{'price':100}},'slot',{})
        self.assertFalse(item['alert'])
        snap['coins'][0]['last_updated']=(NOW-timedelta(hours=1)).isoformat()
        self.assertIsNone(market_event(snap,{'bitcoin':{'price':100}},'slot',{}))


    def test_recovery_supersedes_unsent_incident_alert(self):
        initial = state()
        old = event('old', True)
        old['published_at'] = (NOW - timedelta(hours=1)).isoformat()
        initial['pending_events'] = {'old': {'event': old, 'detected_at': old['published_at']}}
        recovery = event('recovered', False)
        self.events = [recovery]
        updated, ids, summary = prepare(snapshot(), initial, {}, NOW)
        self.assertEqual(summary['superseded_queue_events'], 1)
        self.assertEqual(len(ids), 1)
        self.assertEqual(updated['outbox'][ids[0]]['event_ids'], ['recovered'])

    def test_queued_old_major_event_is_downgraded_to_dated_digest(self):
        initial = state()
        old = event('old', True)
        old['published_at'] = (NOW - timedelta(hours=7)).isoformat()
        initial['pending_events'] = {'old': {'event': old, 'detected_at': old['published_at']}}
        updated, ids, _ = prepare(snapshot(), initial, {}, NOW)
        payload = updated['outbox'][ids[0]]['payload']['content']
        self.assertIn('Delayed report', payload)
        self.assertNotIn('Strict major-event alert', payload)

    def test_new_dynamic_universe_member_gets_comparison_baseline(self):
        snap = snapshot()
        snap['coins'].append({'id':'new-coin', 'symbol':'NEW', 'current_price':10, 'last_updated':NOW.isoformat()})
        updated, ids, _ = prepare(snap, state(), {}, NOW)
        self.assertEqual(updated['digest_baseline']['new-coin']['price'], 10)
        self.assertEqual(ids, [])
        snap['coins'][1]['current_price'] = 12
        later = NOW + timedelta(hours=12)
        snap['as_of'] = later.isoformat()
        for coin in snap['coins']: coin['last_updated'] = later.isoformat()
        updated, ids, _ = prepare(snap, updated, {}, later)
        self.assertEqual(len(ids), 1)
        self.assertIn('NEW +20.0%', updated['outbox'][ids[0]]['payload']['content'])

    def test_corrupt_detection_history_cannot_silently_bootstrap(self):
        bad = state(); bad['engine'] = {}
        with self.assertRaisesRegex(ValueError, 'lost detection history'):
            prepare(snapshot(), bad, {}, NOW)


    def test_official_upgrade_removes_prior_queued_secondary_lead(self):
        initial = state()
        old = event('secondary', False)
        old['sources'] = [{'name':'Secondary', 'url':'https://decrypt.co/example'}]
        initial['pending_events'] = {'secondary': {'event': old, 'detected_at': NOW.isoformat()}}
        official = event('official', True)
        official['supersedes'] = ['secondary']
        self.events = [official]
        updated, ids, summary = prepare(snapshot(), initial, {}, NOW)
        self.assertEqual(len(ids), 1)
        self.assertEqual(updated['outbox'][ids[0]]['event_ids'], ['official'])
        self.assertEqual(updated['pending_events'], {})
        self.assertEqual(summary['superseded_queue_events'], 1)

    def test_digest_overflow_waits_for_next_slot(self):
        self.events = [event(str(n), False) for n in range(7)]
        updated, ids, _ = prepare(snapshot(), state(), {}, NOW)
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(updated['pending_events']), 2)
        self.events = []
        _, ids, _ = prepare(snapshot(), updated, {}, NOW + timedelta(minutes=17))
        self.assertEqual(ids, [])


if __name__=='__main__': unittest.main()

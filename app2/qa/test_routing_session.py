"""Offline checks for persistent routing context, memory and uncertain retries."""
import json
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from routing import Classifier, RouterProcess, chat_input, parse_decision
from routing_memory import load_memory, save_edits


def output(recipient='gpt', remember=None, forget=None):
    decision = {'recipient': recipient, 'remember': remember or [], 'forget': forget or []}
    return {'result': json.dumps(decision), 'usage': {'cache_read_input_tokens': 123},
            'total_cost_usd': 0.01}


class RoutingSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.room = {'dir': self.tmp.name}
        self.records = []

    def tearDown(self):
        self.tmp.cleanup()

    def classifier(self):
        return Classifier(lambda: 'claude.exe', ROOT/'prompts/coordinator-memory.md',
                          lambda *a, **kw: self.records.append(kw), lambda: {})

    def state(self):
        return json.loads((Path(self.tmp.name)/'routing-session.json').read_text())

    def test_resume_after_client_restart_only_appends_new_public_messages(self):
        events = [{'seq': i, 'from': 'user', 'text': f'turn {i}'} for i in range(1, 21)]
        with patch('routing.RouterProcess') as run:
            run.return_value.request.return_value=output()
            self.classifier().choose(self.room, events)
            first = run.call_args
            first_payload = run.return_value.request.call_args.args[0]
            identity = self.state()['session_id']
            self.assertEqual(len(json.loads(first_payload)['chat']), 20)
            events += [{'seq':21, 'from':'claude', 'role':'working', 'text':'private'},
                       {'seq':22, 'from':'gpt', 'text':'answer'},
                       {'seq':23, 'from':'user', 'text':'next question'}]
            self.classifier().choose(self.room, events, {'gpt':'busy','claude':'idle'})
            second = run.call_args
        command = second.args[0]
        self.assertEqual(command[command.index('--resume')+1], identity)
        self.assertEqual(first.args[1], second.args[1])
        payload = json.loads(run.return_value.request.call_args.args[0])
        self.assertEqual([e['seq'] for e in payload['chat']], [22,23])
        self.assertEqual(payload['availability']['gpt'], 'busy')
        self.assertEqual(self.state()['cursor'], 23)
        self.assertEqual(self.records[-1]['usage']['cache_read_input_tokens'],123)

    def test_consecutive_turns_reuse_process_and_cost_is_incremental(self):
        events=[{'seq':1,'from':'user','text':'first'}]
        classifier=self.classifier()
        with patch('routing.RouterProcess') as process:
            process.return_value.request.side_effect=[output(), {**output(), 'total_cost_usd':0.015}]
            classifier.choose(self.room,events)
            events.append({'seq':2,'from':'user','text':'second'})
            classifier.choose(self.room,events)
            process.assert_called_once()
            self.assertEqual(process.return_value.request.call_count,2)
            self.assertAlmostEqual(self.records[-1]['cost_usd'],0.005)
            classifier.close()
            process.return_value.close.assert_called_once()

    def test_stream_transport_ignores_metadata_and_reports_eof(self):
        event={'type':'result', **output()}
        stream=io.BytesIO((json.dumps({'type':'system','subtype':'init'})+'\n'+json.dumps(event)+'\n').encode())
        child=SimpleNamespace(stdout=stream,stdin=io.BytesIO(),poll=lambda:None)
        with patch('routing.subprocess.Popen',return_value=child):
            channel=RouterProcess(['cli'],self.tmp.name,{})
        result=channel.request('hello')
        self.assertEqual(result,event)
        self.assertEqual(json.loads(child.stdin.getvalue())['message']['content'],'hello')
        with self.assertRaisesRegex(RuntimeError,'连接中断'):
            channel.request('again')

    def test_explicit_preference_survives_new_classifier_and_later_chat(self):
        rule = {'key':'independent_topic', 'text':'Send unrelated questions to the idle assistant.'}
        events = [{'seq':1, 'from':'user', 'text':'Remember this routing preference.'}]
        with patch('routing.RouterProcess') as run:
            run.return_value.request.return_value=output(remember=[rule])
            self.classifier().choose(self.room, events)
        memory = load_memory(self.room)
        self.assertEqual(memory['rules'][rule['key']]['source_seq'], 1)
        events += [{'seq':i, 'from':'user', 'text':'ordinary question'} for i in range(2,42)]
        with patch('routing.RouterProcess') as run:
            run.return_value.request.return_value=output()
            self.classifier().choose(self.room,events)
        self.assertEqual(json.loads(run.return_value.request.call_args.args[0])['routing_memory'],memory['rules'])
        save_edits(self.room,memory,events[-1],[],[rule['key']])
        self.assertEqual(load_memory(self.room)['rules'],{})

    def test_invalid_result_keeps_cursor_and_memory_then_rebuilds_uncertain_session(self):
        events = [{'seq':1,'from':'user','text':'first'}]
        with patch('routing.RouterProcess') as run:
            run.return_value.request.return_value=output()
            self.classifier().choose(self.room,events)
        identity = self.state()['session_id']
        events.append({'seq':2,'from':'user','text':'second'})
        with patch('routing.RouterProcess') as run:
            run.return_value.request.return_value=output('unknown')
            with self.assertRaises(ValueError):
                self.classifier().choose(self.room,events)
        self.assertEqual(self.state()['cursor'],1)
        self.assertTrue(self.state()['pending'])
        self.assertEqual(load_memory(self.room)['rules'],{})
        with patch('routing.RouterProcess') as run:
            run.return_value.request.return_value=output()
            self.classifier().choose(self.room,events)
        self.assertNotEqual(self.state()['session_id'],identity)
        self.assertIn('--session-id',run.call_args.args[0])
        self.assertEqual(len(json.loads(run.return_value.request.call_args.args[0])['chat']),2)

    def test_corrupt_memory_is_not_silently_erased(self):
        path = Path(self.tmp.name)/'routing-memory.json'
        path.write_text('{broken')
        with patch('routing.RouterProcess') as run, self.assertRaises(ValueError):
            self.classifier().choose(self.room,[{'seq':1,'from':'user','text':'hi'}])
        run.assert_not_called()
        self.assertEqual(path.read_text(),'{broken')

    def test_memory_requires_user_source_and_valid_unique_edits(self):
        rule={'key':'x','text':'a preference'}
        with self.assertRaises(ValueError):
            save_edits(self.room,load_memory(self.room),{'seq':1,'from':'claude'},[rule],[])
        with self.assertRaises(ValueError):
            parse_decision(json.dumps({'recipient':'gpt','remember':[rule], 'forget':['x']}))
        self.assertFalse((Path(self.tmp.name)/'routing-memory.json').exists())


if __name__ == '__main__':
    unittest.main()

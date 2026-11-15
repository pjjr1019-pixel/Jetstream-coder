"""Synthetic cloud peer using the production proposal adapter and host pipes."""
import json
import sys
import threading

from deskhand_agent_session import run_agent_session
from deskhand_cloud_tools import HostToolChat
from deskhand_dispatch import Reply, Request, Route
from deskhand_host_rpc import HostRpc
from pathlib import Path
import argparse

output_lock = threading.Lock()
def emit(value):
    with output_lock:
        print(json.dumps(value), flush=True)

host = HostRpc(emit, timeout=30, action_timeout=150)
parser = argparse.ArgumentParser()
parser.add_argument('--data', type=Path, required=True)
args = parser.parse_args() if __name__ == '__main__' else None

def task(command):
    request = Request(command['id'], command['text'], Route(*(command[key] for key in ('assistant', 'model', 'account', 'effort'))), command['agent_task_id'])
    count = 0
    def provider(inner):
        nonlocal count
        count += 1
        envelope = json.loads(inner.text.split('\n', 1)[1])
        if count == 1:
            response = {'tool': {'name': 'command.run', 'arguments': {'command': 'echo needs review' if request.text == 'review' else 'echo DESKHAND_COMMAND_FIXTURE', 'purpose': 'Verify shared cloud permission routing'}}}
        else:
            response = {'final': 'Host command confirmed'}
        return Reply(inner.turn_id, inner.route, json.dumps({'exchange_id': envelope['exchange_id'], **response}))
    def run(req, adapter):
        result = run_agent_session(task_id=req.agent_task_id, turn_id=req.turn_id, adapter=adapter, host=host, stopped=lambda: False)
        host.call('task.end', {'task_id': req.agent_task_id, 'turn_id': req.turn_id, 'outcome': result.outcome})
        return Reply(req.turn_id, req.route, result.text)
    try:
        result = HostToolChat(provider, run, args.data / 'cloud-receipts.db')(request)
        emit({'rpc': command['rpc'], 'ok': True, 'result': {'text': result.text, 'rounds': count}})
    except Exception as error:
        emit({'rpc': command['rpc'], 'ok': False, 'error': str(error)})

if __name__ == '__main__':
    for line in sys.stdin:
        command = json.loads(line)
        op = command.get('op')
        if op == 'host_result':
            host.receive(command.get('host_call'), command.get('result'))
        elif op == 'submit':
            threading.Thread(target=task, args=(command,), daemon=True).start()
        else:
            result = ({'muse_stop': 'not_enabled'} if op == 'stop_muse' else
                      {'routes': [{'assistant': 'codex', 'account': 'default'}, {'assistant': 'muse', 'account': 'deskhand'}]} if op == 'catalog' else
                      {'ready': True} if op == 'access_validate' else {'turns': []})
            emit({'rpc': command['rpc'], 'ok': True, 'result': result})

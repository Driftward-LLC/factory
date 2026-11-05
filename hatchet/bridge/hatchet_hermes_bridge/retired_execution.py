"""Read-only retirement descriptors from existing root-attested resolution records.

Not a new recovery decision or executor receipt. Only the root lifecycle wrapper
uses these bounded records, after proving the exact original Docker object absent.
"""
import hashlib
import json
import re
from pathlib import Path
import subprocess

from . import interrupted_resolution as resolution, store


def fingerprint(job):
    module = Path('/app/hatchet/workers/bridge-executor-worker/dist/hermes/invocation-journal.js')
    code = "let raw='';process.stdin.on('data',v=>raw+=v);process.stdin.on('end',()=>process.stdout.write(require(process.argv[1]).invocationFingerprint(JSON.parse(raw),2)))"
    result = subprocess.run(['node', '-e', code, str(module)], input=store.canonical_json(job), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True)
    if not re.fullmatch('[a-f0-9]{64}',result.stdout):
        raise ValueError('invalid invocation fingerprint')
    return result.stdout


def descriptors(conn, fingerprint_job=fingerprint):
    rows = conn.execute("SELECT * FROM factory_recovery_requests WHERE state=? ORDER BY request_key LIMIT 65", (resolution.EVENT,)).fetchall()
    if len(rows) > 64: raise ValueError('retirement descriptor budget exceeded')
    output = []
    for row in rows:
        record = json.loads(row['evidence_json'])
        if store.payload_hash(record) != row['evidence_digest']: raise ValueError('resolution record digest mismatch')
        intent = record['intent']
        attestation = resolution._attestation(intent['attestation'])
        if attestation['containment_method'] != 'isolated_runtime_retired': continue
        if resolution._principal(row['actor']) != intent['principal'] or intent['principal'] != 'uid:0':
            raise ValueError('resolution root principal mismatch')
        attempt = store.get_factory_attempt(conn, row['attempt_id'])
        job = store.get_job(conn, row['job_id'])
        lease = conn.execute('SELECT * FROM factory_execution_leases WHERE attempt_id=?', (row['attempt_id'],)).fetchone()
        if (attempt['job_id'] != job['job_id'] or attempt['mode'] != 'real' or attempt['status'] != 'failed'
                or job['requested_action'] not in store.DRAFT_PR_EFFECT_ACTIONS or job['payload'].get('dry_run') is not False
                or job['state'] != 'failed' or job['reason_code'] != 'operator_interrupted_outcome_unknown'
                or not lease or not lease['released_at'] or not lease['effect_authorized_at'] or not lease['cancel_requested_at'] or lease['effect_observed_at']
                or intent['attempt_id'] != attempt['attempt_id']
                or any(attestation[key] != expected for key,expected in {'attempt_id':attempt['attempt_id'],'job_id':job['job_id'],'lease_id':lease['lease_id'],'lease_epoch':lease['epoch']}.items())):
            raise ValueError('resolution does not bind failed unknown execution')
        bound = store.get_execution_runtime(conn, job['job_id'])
        if not bound or bound['lease_id'] != lease['lease_id'] or bound['epoch'] != lease['epoch']:
            raise ValueError('missing immutable runtime binding')
        runtime = store._execution_runtime(bound['runtime'])
        if runtime['backend'] != 'hermes' or attestation['runtime_ref'] != runtime['instance_id']:
            raise ValueError('retirement does not identify exact runtime')
        event = conn.execute('SELECT details_json FROM factory_attempt_events WHERE attempt_id=? AND event_type=? AND json_extract(details_json,\'$.request_id\')=?',
                             (attempt['attempt_id'],resolution.EVENT,row['request_key'])).fetchall()
        if len(event) != 1: raise ValueError('resolution audit event missing or ambiguous')
        audit = json.loads(event[0]['details_json'])
        if (audit.get('actor') != row['actor'] or audit.get('evidence_digest') != row['evidence_digest']
                or audit.get('snapshot_digest') != intent['snapshot_digest'] or audit.get('effect_outcome') != 'unknown'
                or audit.get('containment_basis') != 'trusted_root_attestation_not_machine_verified'):
            raise ValueError('resolution audit mismatch')
        result = json.loads(row['result_json'])
        if any(result.get(key) != expected for key,expected in {'attempt_id':attempt['attempt_id'],'job_id':job['job_id'],'effect_outcome':'unknown','resolution':resolution.EVENT}.items()):
            raise ValueError('resolution result mismatch')
        output.append({'job_id':job['job_id'],'runtime':runtime,'fingerprint_version':2,'fingerprint':fingerprint_job(job),
                       'lease_id':lease['lease_id'],'owner':lease['owner'],'epoch':lease['epoch'],
                       'fence_sha256':hashlib.sha256(lease['fence_token'].encode()).hexdigest()})
    return output


if __name__ == '__main__':
    with store.connect_readonly() as conn:
        if store.list_active_factory_attempts(conn): raise SystemExit(73)
        value = json.dumps(descriptors(conn),sort_keys=True,separators=(',',':'))
        if len(value)>65536: raise SystemExit(73)
        print(value)

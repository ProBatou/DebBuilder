import test from 'node:test';
import assert from 'node:assert/strict';
import {admitDraftRun} from '../src/features/recipes/admission.js';

const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers:{'Content-Type':'application/json'}});

test('Test and Build validate and submit the same frozen draft without saving', async () => {
  for (const dryRun of [true, false]) {
    const editor = {draft:{schema_version:5,name:'demo',active:true,build:{commands:['npm run build:prod']}}};
    const calls = [];
    let release;
    const pendingValidation = new Promise(resolve => release = resolve);
    const fetchImpl = async (path, options) => {
      calls.push({path,body:JSON.parse(options.body)});
      if (path === '/api/recipes/validate') {await pendingValidation; return json({valid:true});}
      return json({run_id:dryRun ? 'run-test-123' : 'run-build-456',status:'queued'},202);
    };
    const pending = admitDraftRun(editor,dryRun,{fetchImpl});
    editor.draft.build.commands[0] = 'npm run changed-after-click';
    release();
    assert.equal(await pending,dryRun ? 'run-test-123' : 'run-build-456');
    assert.deepEqual(calls.map(call => call.path),['/api/recipes/validate','/api/run']);
    assert.deepEqual(calls[0].body.recipe,calls[1].body.workflow);
    assert.equal(calls[1].body.workflow.build.commands[0],'npm run build:prod');
    assert.equal(calls[1].body.dry_run,dryRun);
  }
});

test('validation rejection prevents Run admission', async () => {
  const calls = [];
  const fetchImpl = async (path) => {calls.push(path); return json({error:{code:'invalid_recipe',message:'Invalid Recipe',path:'$.build.commands'}},422);};
  await assert.rejects(admitDraftRun({draft:{name:'demo'}},true,{fetchImpl}),error => error.status === 422 && error.path === '$.build.commands');
  assert.deepEqual(calls,['/api/recipes/validate']);
});

test('ambiguous network outcome is not retried', async () => {
  const calls = [];
  const fetchImpl = async path => {
    calls.push(path);
    if (path === '/api/recipes/validate') return json({valid:true});
    throw new TypeError('Connection lost after acceptance');
  };
  await assert.rejects(admitDraftRun({draft:{name:'demo'}},false,{fetchImpl}),error => error.kind === 'network');
  assert.deepEqual(calls,['/api/recipes/validate','/api/run']);
});

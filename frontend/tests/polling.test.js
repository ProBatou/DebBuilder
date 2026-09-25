import test from 'node:test';
import assert from 'node:assert/strict';
import {createPoller} from '../src/features/polling.js';

const delay = ms => new Promise(resolve => setTimeout(resolve,ms));

test('mount starts, stop aborts, no overlap, and restart has one timer', async () => {
  let calls=0, concurrent=0, maximum=0, aborted=0, resolve;
  const poller=createPoller(signal=>{calls++; concurrent++; maximum=Math.max(maximum,concurrent); signal.addEventListener('abort',()=>{aborted++; concurrent--;}); return new Promise(done=>resolve=value=>{concurrent--;done(value);});},{interval:5});
  poller.start(); poller.start(); assert.equal(calls,1);
  await delay(20); assert.equal(calls,1); assert.equal(maximum,1);
  poller.stop(); assert.equal(aborted,1); resolve('stale'); await delay(15); assert.equal(calls,1);
  poller.start(); assert.equal(calls,2); poller.stop();
});

test('transient error retries and retains last rendered value', async () => {
  let calls=0, current='old', failures=0, resolve;
  const recovered = new Promise(done => resolve = done);
  const poller=createPoller(async()=>{calls++; if(calls===2) throw Error('temporary'); return `value-${calls}`;},{interval:5,retry:5,onData:value=>{current=value; if(calls===3) resolve();},onError:()=>{failures++; assert.equal(current,'value-1');}});
  poller.start();
  try {await Promise.race([recovered, delay(500).then(()=>{throw Error('poll did not retry');})]); assert.equal(failures,1); assert.equal(current,'value-3');}
  finally {poller.stop();}
});

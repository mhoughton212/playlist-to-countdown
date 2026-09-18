const test = require('node:test');
const assert = require('node:assert/strict');
const {adjust} = require('../countdown/ui/clip-range.js');

test('locked end moves start back by the same amount', () => {
  assert.deepEqual(adjust({start:40.7,end:50.7}, 'end', 49.7, true), {start:39.7,end:49.7,duration:10});
});
test('locked start moves end without changing the length', () => {
  assert.deepEqual(adjust({start:40.5,end:50.5}, 'start', 40.7, true), {start:40.7,end:50.7,duration:10});
});
test('unlocked end keeps start and changes duration', () => {
  assert.deepEqual(adjust({start:40.7,end:50.7}, 'end', 49.7, false), {start:40.7,end:49.7,duration:9});
});
test('unlocked start keeps end and changes duration', () => {
  assert.deepEqual(adjust({start:40.7,end:50.7}, 'start', 41.7, false), {start:41.7,end:50.7,duration:9});
});
test('explicit length changes anchor at the start, locked or unlocked', () => {
  for (const locked of [true,false]) assert.deepEqual(adjust({start:40.7,end:50.7}, 'duration', 12.5, locked), {start:40.7,end:53.2,duration:12.5});
});
test('invalid ranges are rejected without mutating the saved range', () => {
  const current = {start:5,end:15};
  assert.throws(() => adjust(current,'end',9,true), /before 0:00/);
  assert.throws(() => adjust(current,'end',5,false), /0.5 to 120/);
  assert.throws(() => adjust(current,'start',16,false), /0.5 to 120/);
  assert.throws(() => adjust(current,'duration',NaN,true), /valid clip time/);
  assert.deepEqual(current,{start:5,end:15});
});

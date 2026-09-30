// The transcript's wording, checked with Node's own test runner (no
// dependency): `npm test` in frontend/.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { duration, groupPhrases, stepLine, toolArgs } from './activity.js'

test('durations round the whole span before splitting it into units', () => {
    assert.equal(duration(0, 1.9), 'briefly')
    assert.equal(duration(0, 12.4), 'for 12s')
    assert.equal(duration(0, 59.5), 'for 1m') // not "for 60s"
    assert.equal(duration(0, 119.5), 'for 2m') // not "for 1m 60s"
    assert.equal(duration(0, 190), 'for 3m 10s')
    assert.equal(duration(null, 5), null) // a transcript from before ts
})

test('a step names what it acted on whether its args are structured or a JSON string', () => {
    const structured = { name: 'terminal', args: { command: 'pwd && ls' } }
    const legacy = { name: 'terminal', args: '{"command": "pwd && ls"}' }
    assert.deepEqual(stepLine(legacy), stepLine(structured))
    assert.equal(stepLine(legacy).subject, 'pwd && ls')
    const write = { name: 'file_write', args: '{"path": "/workspace/a.txt"}' }
    assert.equal(stepLine(write).subject, '/workspace/a.txt')
    // a Python-repr string from before structured args is left for the
    // generic view rather than guessed at
    assert.equal(toolArgs({ name: 'terminal', args: "{'command': 'ls'}" }), null)
})

test('a group counts files once however often they were written, JSON-string args included', () => {
    const tools = [
        { name: 'terminal', args: { command: 'ls' } },
        { name: 'file_write', args: { path: '/a' } },
        { name: 'file_write', args: '{"path": "/a"}' },
        { name: 'file_write', args: { path: '/b' } },
    ]
    assert.deepEqual(groupPhrases(tools), [
        { verb: 'Ran', rest: '1 command' },
        { verb: 'wrote', rest: '2 files' },
    ])
})

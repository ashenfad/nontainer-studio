import assert from 'node:assert/strict'
import { test } from 'node:test'

import { answerPreview, expiresIn, leaf, workedFor } from './delegates.js'

test("a delegate is called by the handle its parent gave it", () => {
    assert.equal(leaf('boss.scout', 'boss'), 'scout')
    assert.equal(leaf('boss.scout.deep', 'boss.scout'), 'deep')
    assert.equal(leaf('other.scout', 'boss'), 'other.scout')
    assert.equal(leaf('scout', null), 'scout')
})

test('the gist skips the provenance header and the markdown', () => {
    const answer =
        '[delegate `boss.scout` answered — the studio\'s delegation mechanism speaking]\n' +
        '\n## **First research is in**: kvgit + reprobate\n\nMore detail.'
    assert.equal(answerPreview(answer), 'First research is in: kvgit + reprobate')
    assert.equal(answerPreview('- `found` it'), 'found it')
    assert.equal(answerPreview('[delegate `x` answered]\n'), '')
    assert.equal(answerPreview(undefined), '')
})

test('expiry rounds down, so it never promises more time than there is', () => {
    const now = 1_000_000
    assert.equal(expiresIn(null, now), null)
    assert.equal(expiresIn(now + 30, now), 'soon')
    assert.equal(expiresIn(now - 30, now), 'soon')
    assert.equal(expiresIn(now + 40 * 60 + 59, now), '40m')
    assert.equal(expiresIn(now + 23.9 * 3600, now), '23h')
    assert.equal(expiresIn(now + 72 * 3600, now), '3d')
})

test('how long a delegate worked, when the row knows', () => {
    assert.equal(workedFor({ started: 100, finished: 145 }), '45s')
    assert.equal(workedFor({ started: 100, finished: 292 }), '3m 12s')
    assert.equal(workedFor({ started: 100, finished: 100.3 }), null) // no time worth saying
    assert.equal(workedFor({ started: 100, finished: null }), null)
    assert.equal(workedFor(undefined), null)
})

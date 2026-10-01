// `npm test` in frontend/.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { latestOnly } from './latest.js'

test('an older call that settles last is superseded', async () => {
    const resolvers = []
    const slow = latestOnly((label) => new Promise((resolve) => resolvers.push(() => resolve(label))))
    const first = slow('before delivery')
    const second = slow('after delivery')
    resolvers[1]() // the newer request answers first
    resolvers[0]() // the older one last
    assert.equal(await second, 'after delivery')
    assert.equal(await first, latestOnly.SUPERSEDED)
})

test('calls that do not overlap each get their value', async () => {
    const echo = latestOnly(async (x) => x)
    assert.equal(await echo(1), 1)
    assert.equal(await echo(2), 2)
})

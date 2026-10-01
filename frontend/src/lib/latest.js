// Wrap an async function so that only its most recent call's result is
// used. An earlier call that settles after a later one was started
// resolves to `latestOnly.SUPERSEDED` instead of its value, so a caller
// polling and refreshing at once never applies an older answer over a
// newer one.
export function latestOnly(fn) {
    let latest = 0
    return async (...args) => {
        const mine = ++latest
        const value = await fn(...args)
        return mine === latest ? value : latestOnly.SUPERSEDED
    }
}

latestOnly.SUPERSEDED = Symbol('superseded')

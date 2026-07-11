// The shared file-viewer state: one FileModal instance (mounted at the
// App level) reachable from anywhere a workspace path appears — the
// files tab, chat markdown links, tool-timeline thumbnails.

export const viewer = $state({ path: null })

export function viewFile(path) {
    viewer.path = path
}

export function closeViewer() {
    viewer.path = null
}

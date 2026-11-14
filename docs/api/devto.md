# Posts API (moved)

This page was about the dev.to-only version. Posts now sends one draft to many destinations (dev.to, Hashnode, WordPress, Ghost, a markdown folder, a webhook, and manual sites such as
LinkedIn, Medium, X and Reddit). The full reference is **[destinations.md](destinations.md)**.

The old `/api/devto/*` routes still work as thin shims over the new engine (they act on the dev.to destination only):
`status`, `key` (PUT/DELETE), `test`, `settings`, `ideas`, `ideas/refresh`, `drafts` (CRUD), `drafts/{id}/save`, `drafts/{id}/publish {confirm: true}`, `drafts/{id}/pack`, `polish`, `published`, `refresh`,
`comments/ack`. New code should use `/api/posts` and `/api/destinations`.

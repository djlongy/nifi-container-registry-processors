---
title: What the webhook is trusted for
type: decision
status: accepted
updated: 2026-10-14
---

# ADR 0005: What the webhook is trusted for

Behaviour claims are Unverified until tested.

## Context

The sender (gitlab-registry-mirror) posts a JSON body and headers. Some headers (`X-CI-Project`, `X-CI-Pipeline-Url`, `X-CI-Commit-Sha`, `X-Image-Count`, `X-Registry`) describe the pipeline.

## Options

| Option | Pros | Cons |
|---|---|---|
| Body, `X-Sha256` and the registry's digest are the authority; other headers are display only | Small trusted surface. A forged or stale header cannot select or approve an image | Headers cannot be used to cross-check the body |
| Also check headers against the body (count, registry) | Catches some sender bugs | Adds a second source of truth and new ways to fail a valid webhook. Headers are not covered by `X-Sha256` |
| Put the image list in a header | Routable without parsing | Header size limits cap the list. The list would also sit outside the hashed body |

## Decision

The body selects images. Export accepts it only if it matches `X-Sha256`, names the host of Source Registry URL, lists only Allowed Repositories, and every digest equals what the registry resolves now. The `X-CI-*`, `X-Image-Count` and `X-Registry` headers stay on the FlowFile as attributes for routing and display and are never used to select or check images.

## Consequences

- A header that disagrees with the body changes nothing. A test sends `X-Image-Count: 99` and `X-Registry: evil.example` and the export still succeeds with the real count and host.
- `X-Sha256` proves the body is intact, not who sent it. Authenticate the sender on ListenHTTP (a token or TLS client authentication) if that matters.

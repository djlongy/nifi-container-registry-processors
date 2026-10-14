---
title: One tar for the webhook or one tar per image
type: decision
status: accepted
updated: 2026-10-14
---

# ADR 0004: One tar for the webhook or one tar per image

Measured on a lab NiFi 2.11: with 2 images sharing 4 of 5 layers, Tar Per Image `true` sends 1.92 times the bytes of one tar.

## Context

A webhook can name several images. Export can write them as one tar or as one tar each (property Tar Per Image, default `false`).

## Options

| Option | Pros | Cons |
|---|---|---|
| One tar (default) | Shared layers are stored once, so fewer bytes cross the diode. One FlowFile, one Push | Push is all or nothing for the bundle. Cannot route one image on its own |
| One tar per image | Each image can be routed, retried and tracked on its own | Shared base layers repeat in every tar, so more bytes cross the diode. Needs SplitText and EvaluateJsonPath downstream |

## Decision

Default to one tar. Tar Per Image `true` is available.

## Consequences

- A Python FlowFileTransform returns exactly one FlowFile (NiFi source, `FlowFileTransformProxy.onTrigger`). With Tar Per Image `true` the success FlowFile holds one JSON line per tar, and the README shows the SplitText plus EvaluateJsonPath pattern that turns each line into a FlowFile.
- Max Export Size applies to each tar and counts each distinct blob digest once, so in one-tar mode a layer shared by two images counts once (measured: a 48.3 MB bundle was refused at 60 MB when shared layers counted twice). If a bundle is over the limit, Export fails the whole webhook. It does not split a bundle into several tars. Splitting would need a new decision.
- All checks run on the whole webhook first. If any image fails in a way Export handles, nothing is exported and any tar already written is removed. After a crash or kill, `.<name>.partial` can remain (measured).

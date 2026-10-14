---
title: Tar directory set by a parameter
type: decision
status: accepted
updated: 2026-10-14
---

# ADR 0003: Tar directory set by a parameter

Behaviour claims are Unverified until tested.

## Context

Tar Output Directory (Pull, Export) and Input Tar Path (Push) name a path on each node. The path has to match a volume mount.

## Options

| Option | Pros | Cons |
|---|---|---|
| Parameter, `#{container-images.tar.dir}` | Changed in the UI, no NiFi restart: NiFi stops the processors that reference it and starts them again (Unverified until tested). One value for Export and Push. Needs no environment on the node | The value can drift from the real volume mount. A wrong value is found only when a run fails |
| Environment variable, `${TAR_DIR}` | Set where the volume is mounted, so it follows the deployment. Both processors read it | Changing it needs a NiFi restart. Input Tar Path on Push evaluates FlowFile attributes, not the environment, so Push cannot use it for the path |
| Hardcoded path | Simplest | Every change edits each processor, and a test and a production flow cannot differ without a copy |

## Decision

A parameter in a parameter context bound to the process group.

## Consequences

- QUICKSTART and the by-hand runbook create `container-images.tar.dir` in the `registry` context.
- Check the value against the mount when deploying. The testing checklist includes this.

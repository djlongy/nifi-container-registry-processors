---
title: Python processors only
type: decision
status: accepted
updated: 2026-10-14
---

# ADR 0001: Python processors only

Claims about NiFi behaviour here are Unverified until tested.

## Context

The repository started with Java processors packaged as a NAR. The same processors were then written as NiFi Python processors (`python/container_registry/`).

## Options

| Option | Pros | Cons |
|---|---|---|
| Java NAR | FlowFile content is streamed, so a multi-GB tar can travel as content. Compiled and typed | Needs Java 21 and Maven to build, a build for each NiFi version, and a release step. Harder to read and change on a locked-down work machine |
| Python processors | Plain source, no build, no dependencies. A person or a local LLM can read, change and test the code with `python3 -m unittest`. Reload with a processor restart | The Python API holds FlowFile content in memory, so the tar is written to disk and passed by path. Needs Python 3.10+ enabled on every node |
| Both | Each covers the other's weakness | Two codebases to keep in step, and two sets of property names |

## Decision

Python only. The reasons given by the maintainer: transparency, and that a local LLM at work can edit and test the code without compiling anything.

## Consequences

- The tar lives on disk, not in FlowFile content. See [ADR 0002](adr-0002-tar-storage.md).
- The Python processors are the maintained implementation. The README describes only them.
- Versions follow the Python processors: 1.5.0 is the first release that ships them with the renamed properties.

# Remove the tar processors

## Context

Option O1 of [ADR 0006](adr-0006-moving-large-images-through-nifi.md) used three Python processors: PullContainerImages, ExportContainerImages and PushContainerImages. Pull or Export wrote one tar to a directory on the node, for example `/opt/nifi/data`. FetchFile then copied the tar into the content repository.

Each image therefore sat on disk twice. The staging copy was outside the storage NiFi manages, so content repository limits, archive cleanup and back pressure did not cover it. The writer and the reader had to run on the same node, or the directory had to be shared storage. Every node needed a staging volume.

The owner wants image bytes only in NiFi's native FlowFile content repository.

## Decision

O1 is removed from 1.6.0. The Python processors are now PlanRegistryTransfer and RouteRegistryTransfer only. With native InvokeHTTP they move one FlowFile per blob and manifest ([ADR 0007](adr-0007-compact-native-layer-transfer.md)). This is the only Python option.

O2, O3 and O4 stayed in `docs/archive/` as native alternatives until 2026-11-23, when the archive and their scripts were removed as unused. They remain in git history.

This supersedes the "O1 is the default" decision of ADR 0006.

## Consequences

The mirror.py export tar is no longer read or written. A NiFi flow cannot hand a tar to `mirror.py import`, or take one from `mirror.py export`. There is no tar for offline or removable-media transfer.

Flows that use Pull, Export or Push stay on the 1.5.x processors. Installing 1.6.0 over the extension directory removes those three from that NiFi, so rebuild such a flow on Plan and Route first, or keep that NiFi on 1.5.x.

The decisions about the tar (ADR 0002, ADR 0003 and ADR 0004) and the tar flow runbook moved to the archive, removed on 2026-11-23 (in git history).

The compact flow evidence so far is small images: BusyBox with a 2.2 MB layer, and an alpine 3.20 index on `registry:2`. The build guide also records one synthetic image with a 1 GiB layer. Multi-GB validation of the compact flow is open.

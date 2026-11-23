# Documentation map

**To build the transfer across a data diode, follow one guide: [the image transfer build guide](runbooks/image-transfer-build-by-hand.md)**, split into a low-side and a high-side part with live canvas screenshots. Each side lists its own properties changed from default, connections, contexts and recovery steps. Designs the project did not adopt were removed on 2026-11-23 and remain in git history.

For what each processor requires of its input, read the [input contract](reference/contract.md). Start with [QUICKSTART.md](../QUICKSTART.md) to move one image. The [top-level README](../README.md) has the property tables and the flow in short.

Claims about NiFi behaviour in `decisions/` and `runbooks/testing-checklist.md` are marked "Unverified until tested" until they have been run on a real NiFi. Measured claims say what was measured. Facts quoted from the NiFi 2.x documentation say so.

| File | What it is |
|---|---|
| [runbooks/image-transfer-build-by-hand.md](runbooks/image-transfer-build-by-hand.md) | The supported diode transfer build: separate low-side and high-side build guides: screenshots, changed-from-default properties, connections and recovery |
| [decisions/adr-0001-python-only.md](decisions/adr-0001-python-only.md) | Why the Python processors are the only implementation |
| [decisions/adr-0005-webhook-trust.md](decisions/adr-0005-webhook-trust.md) | What the webhook is trusted for, and what its headers are not |
| [decisions/adr-0006-moving-large-images-through-nifi.md](decisions/adr-0006-moving-large-images-through-nifi.md) | Why Python cannot stream, and the four proven options (staging dir, layer per FlowFile, loopback ListenHTTP, push from stdin) |
| [decisions/adr-0007-compact-native-layer-transfer.md](decisions/adr-0007-compact-native-layer-transfer.md) | The compact native layer transfer: PlanRegistryTransfer and RouteRegistryTransfer around native InvokeHTTP |
| [decisions/adr-0008-remove-tar-processors.md](decisions/adr-0008-remove-tar-processors.md) | Why 1.6.0 removes the tar processors (Pull, Export, Push) |
| [decisions/adr-0009-notify-wait-manifests.md](decisions/adr-0009-notify-wait-manifests.md) | Why 1.7.0 holds waiting manifests at Wait, released by Notify, with a bounded retry and a fail queue |
| [runbooks/image-transfer-recovery-proof.md](runbooks/image-transfer-recovery-proof.md) | Live selective/full-image recovery, physical layer removal, hashes and provenance evidence |
| [runbooks/testing-checklist.md](runbooks/testing-checklist.md) | What to check, and where in NiFi, when testing the transfer |
| [reference/properties.md](reference/properties.md) | Where the property tables are |
| [reference/contract.md](reference/contract.md) | Input contract: what each processor requires of its body and attributes, and the exact error and relationship when input is missing or wrong. Includes how to stand in for the webhook with a GenerateFlowFile |
| [reference/attributes.md](reference/attributes.md) | Every attribute and header each processor reads or writes |

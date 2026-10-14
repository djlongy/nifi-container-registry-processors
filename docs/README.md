# Documentation map

For **PLANA compact native streaming**, start with [the complete by-hand build and live layout screenshots](runbooks/nifi-by-hand-compact-layer-flow.md). It lists every property, relationship, connection, context and recovery step.

Start with [QUICKSTART.md](../QUICKSTART.md) to push one image. The [top-level README](../README.md) has the property tables, flows and migration table.

Claims about NiFi behaviour in `decisions/` and `runbooks/testing-checklist.md` are marked "Unverified until tested" until they have been run on a real NiFi; the storage, disk, restart-leftover and Tar Per Image claims carry their lab measurements (NiFi 2.11, 9.66 GB image). Facts quoted from the NiFi 2.x documentation say so.

| File | What it is |
|---|---|
| [decisions/adr-0001-python-only.md](decisions/adr-0001-python-only.md) | Why the Python processors are the only implementation |
| [decisions/adr-0002-tar-storage.md](decisions/adr-0002-tar-storage.md) | Where the export tar lives: options, pros and cons |
| [decisions/adr-0003-directory-by-parameter.md](decisions/adr-0003-directory-by-parameter.md) | Why the tar directory is a parameter |
| [decisions/adr-0004-one-bundle-vs-tar-per-image.md](decisions/adr-0004-one-bundle-vs-tar-per-image.md) | One tar for the webhook, or one tar per image |
| [decisions/adr-0005-webhook-trust.md](decisions/adr-0005-webhook-trust.md) | What the webhook is trusted for, and what its headers are not |
| [decisions/adr-0006-moving-large-images-through-nifi.md](decisions/adr-0006-moving-large-images-through-nifi.md) | Why Python cannot stream, and the four proven options (staging dir, layer per FlowFile, loopback ListenHTTP, push from stdin) |
| [runbooks/plana-recovery-proof.md](runbooks/plana-recovery-proof.md) | Live selective/full-image recovery, physical layer removal, hashes and provenance evidence |
| [runbooks/nifi-by-hand-compact-layer-flow.md](runbooks/nifi-by-hand-compact-layer-flow.md) | PLANA: complete 12-processor build, low/high screenshots, all properties and connections |
| [runbooks/nifi-by-hand.md](runbooks/nifi-by-hand.md) | Click-by-click build of the webhook flow in the NiFi 2.x UI |
| [runbooks/nifi-by-hand-o4-push.md](runbooks/nifi-by-hand-o4-push.md) | Click-by-click O4 flow: ExecuteStreamCommand running `python/scripts/push_from_stdin.py` |
| [runbooks/nifi-by-hand-o2-layer-flow.md](runbooks/nifi-by-hand-o2-layer-flow.md) | O2 flow: one FlowFile per layer with InvokeHTTP, a manifest gate and layer recovery |
| [runbooks/nifi-by-hand-native-layer-flow.md](runbooks/nifi-by-hand-native-layer-flow.md) | Native layer flow: PlanRegistryTransfer plans, InvokeHTTP moves every layer, a manifest gate and resend by layer number. Two importable definitions in [flows/](flows/) |
| [runbooks/nifi-by-hand-o3-loopback.md](runbooks/nifi-by-hand-o3-loopback.md) | O3 flow: a script posting to a loopback ListenHTTP with mTLS, no staging disk |
| [runbooks/testing-checklist.md](runbooks/testing-checklist.md) | What to check, and where in NiFi, when testing each option |
| [reference/properties.md](reference/properties.md) | Where the property tables are |
| [reference/attributes.md](reference/attributes.md) | Every attribute and header each processor reads or writes |

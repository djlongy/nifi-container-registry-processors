# Documentation map

**To build the transfer across a data diode, follow one guide: [the image transfer build guide](runbooks/image-transfer-build-by-hand.md)**, split into a low-side and a high-side part with live canvas screenshots. Each side lists its own properties changed from default, connections, contexts and recovery steps. Designs the project did not adopt are in [archive/](archive/README.md).

For what each processor requires of its input, read the [input contract](reference/contract.md). Start with [QUICKSTART.md](../QUICKSTART.md) to push one image. The [top-level README](../README.md) has the property tables, flows and migration table.

Claims about NiFi behaviour in `decisions/` and `runbooks/testing-checklist.md` are marked "Unverified until tested" until they have been run on a real NiFi; the storage, disk, restart-leftover and Tar Per Image claims carry their lab measurements (NiFi 2.11, 9.66 GB image). Facts quoted from the NiFi 2.x documentation say so.

| File | What it is |
|---|---|
| [runbooks/image-transfer-build-by-hand.md](runbooks/image-transfer-build-by-hand.md) | The supported diode transfer build: separate low-side and high-side build guides: screenshots, changed-from-default properties, connections and recovery |
| [decisions/adr-0001-python-only.md](decisions/adr-0001-python-only.md) | Why the Python processors are the only implementation |
| [decisions/adr-0002-tar-storage.md](decisions/adr-0002-tar-storage.md) | Where the export tar lives: options, pros and cons |
| [decisions/adr-0003-directory-by-parameter.md](decisions/adr-0003-directory-by-parameter.md) | Why the tar directory is a parameter |
| [decisions/adr-0004-one-bundle-vs-tar-per-image.md](decisions/adr-0004-one-bundle-vs-tar-per-image.md) | One tar for the webhook, or one tar per image |
| [decisions/adr-0005-webhook-trust.md](decisions/adr-0005-webhook-trust.md) | What the webhook is trusted for, and what its headers are not |
| [decisions/adr-0006-moving-large-images-through-nifi.md](decisions/adr-0006-moving-large-images-through-nifi.md) | Why Python cannot stream, and the four proven options (staging dir, layer per FlowFile, loopback ListenHTTP, push from stdin) |
| [runbooks/image-transfer-recovery-proof.md](runbooks/image-transfer-recovery-proof.md) | Live selective/full-image recovery, physical layer removal, hashes and provenance evidence |
| [runbooks/nifi-by-hand.md](runbooks/nifi-by-hand.md) | Click-by-click build of the single-NiFi tar flow (Export then Push) in the NiFi 2.x UI. Not the diode build |
| [archive/README.md](archive/README.md) | The native layer flow, O2, O3 and O4 build guides and the native flow definitions: designs not adopted |
| [runbooks/testing-checklist.md](runbooks/testing-checklist.md) | What to check, and where in NiFi, when testing each option |
| [reference/properties.md](reference/properties.md) | Where the property tables are |
| [reference/contract.md](reference/contract.md) | Input contract: what each processor requires of its body and attributes, and the exact error and relationship when input is missing or wrong. Includes how to stand in for the webhook with a GenerateFlowFile |
| [reference/attributes.md](reference/attributes.md) | Every attribute and header each processor reads or writes |

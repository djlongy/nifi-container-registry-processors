# Scripts

Standard-library Python scripts for NiFi ExecuteStreamCommand. The supported diode transfer, the [image transfer flow](../../docs/runbooks/image-transfer-build-by-hand.md), does not use them. They belong to designs that are archived in [docs/archive/](../../docs/archive/README.md) and stay here with their unit tests.

| Script | Used by | What it does |
|---|---|---|
| `push_from_stdin.py` | O4 push ([guide](../../docs/archive/nifi-by-hand-o4-push.md)); also imported by the two scripts below | Pushes a blob or a manifest read from stdin into a registry |
| `manifest_gate.py` | The native layer flow and O2 ([native](../../docs/archive/nifi-by-hand-native-layer-flow.md), [O2](../../docs/archive/nifi-by-hand-o2-layer-flow.md)) | Waits until what a manifest names exists, then PUTs its bytes unchanged. Needs `push_from_stdin.py` beside it |
| `post_blob_loopback.py` | O3 ([guide](../../docs/archive/nifi-by-hand-o3-loopback.md)) | Streams a registry blob into a loopback HTTP server as a chunked POST. Needs `push_from_stdin.py` beside it |

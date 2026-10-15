# Archive

These build guides and flow definitions describe designs the project did not adopt. They stay here as a record of what was proven and why it was set aside, next to the decisions in [../decisions/](../decisions/). The supported way to build the transfer is the [image transfer flow](../runbooks/image-transfer-build-by-hand.md). Nothing here is maintained.

| File | What it is |
|---|---|
| [nifi-by-hand-native-layer-flow.md](nifi-by-hand-native-layer-flow.md) | The first native layer flow: PlanRegistryTransfer plans, InvokeHTTP moves every layer, and a manifest gate script (`manifest_gate.py`) commits manifests. image transfer replaces the many native processors and the gate script with RouteRegistryTransfer |
| [flows/native-layer-flow-low.json](flows/native-layer-flow-low.json), [flows/native-layer-flow-high.json](flows/native-layer-flow-high.json) | Importable process group definitions for the native layer flow |
| [nifi-by-hand-o2-layer-flow.md](nifi-by-hand-o2-layer-flow.md) | O2: one FlowFile per layer moved by InvokeHTTP, with a manifest gate and layer recovery |
| [nifi-by-hand-o3-loopback.md](nifi-by-hand-o3-loopback.md) | O3: a script posts a blob into a loopback ListenHTTP with mutual TLS, so no staging disk is needed |
| [nifi-by-hand-o4-push.md](nifi-by-hand-o4-push.md) | O4: ExecuteStreamCommand runs `push_from_stdin.py` to push a blob or manifest FlowFile |

The scripts these designs use stay in [../../python/scripts/](../../python/scripts/README.md).

# Quickstart

The shortest path to one container image moved through PlanRegistryTransfer and RouteRegistryTransfer, with both sides on one NiFi 2.x instance as a test. The full flow, every property and connection, and the recovery steps are in [the image transfer build guide](docs/runbooks/image-transfer-build-by-hand.md). This page does not repeat them. All hosts and passwords below are placeholders.

You need: NiFi 2.x with Python enabled (`nifi.python.command` set), shell access to the NiFi node, a source and a target registry, and a small image in the source registry.

## 1. Install the processors

On every NiFi node:

```sh
git clone https://github.com/djlongy/nifi-container-registry-processors.git
cp -r nifi-container-registry-processors/python/container-registry /opt/nifi/nifi-current/python_extensions/
```

Use the directory `nifi.python.extensions.source.directory.default` names in `nifi.properties` if it is not `./python_extensions`. Within a minute `PlanRegistryTransfer` and `RouteRegistryTransfer` 1.7.0 appear in **Add Processor** (filter on `registry`). Nothing is installed from PyPI.

## 2. Build the two groups

Make two process groups on one canvas, `ImageTransferLowSide` and `ImageTransferHighSide`, each with its own parameter context:

| Group | Parameters | Build guide |
|---|---|---|
| `ImageTransferLowSide` | `gitlab.container.url`, `.username`, `.token` (sensitive), `.allowed.repositories`, `.platforms` for the source | [Low side](docs/runbooks/image-transfer-build-by-hand.md#low-side), L1 to L7 |
| `ImageTransferHighSide` | `gitlab.container.url`, `.username`, `.token` (sensitive), `.repository` for the target | [High side](docs/runbooks/image-transfer-build-by-hand.md#high-side), H1 to H7 |

Two contexts keep the side-free names apart and keep source credentials out of the high group. With the NiFi MCP server, [compact-layer-low-mcp-spec.json](docs/flows/compact-layer-low-mcp-spec.json) and [compact-layer-high-mcp-spec.json](docs/flows/compact-layer-high-mcp-spec.json) build each group in one call. They carry no credentials or trust services: bind the contexts and set trust afterwards.

On the parent canvas connect the low group's `ToDiode` port to the high group's `FromDiode` port, as in [L8](docs/runbooks/image-transfer-build-by-hand.md#l8-send-across-the-diode). This connection stands in for the diode.

## 3. Start it

Start the high group first, then the low group. Keep the LogMessage (Hold) cards **disabled**: their queues keep failures. Every other card must validate with no red warning icon.

## 4. Send one request

Get the image's real manifest digest from the source registry, then post the request body with its `X-Sha256`. The build guide's [L9, Manual first request](docs/runbooks/image-transfer-build-by-hand.md#l9-publish-the-webhook-and-connect-gitlab) has a script that does both. On one NiFi the URL is `http://<nifi-host>:9617/image-transfer-webhook`.

`HTTP 202` means ListenHTTP accepted the request, not that the transfer worked.

## 5. Check it

```sh
skopeo inspect --raw docker://registry-high.example.com/team/app:1.0 | sha256sum
```

The digest matches the one in the request. Every work queue drains to zero, and the queues into the Hold cards stay empty.

A FlowFile held in front of a Hold card carries `registry.error`: **List queue**, open the row, **Attributes**. The texts and their causes are in the [input contract](docs/reference/contract.md). To resend an image or chosen layers, see [L11](docs/runbooks/image-transfer-build-by-hand.md#l11-recover-a-held-or-incomplete-transfer).

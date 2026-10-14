# Building the O2 layer flow by hand: one FlowFile per layer

Each layer of an image becomes its own FlowFile, moved by `InvokeHTTP`, so Python never holds layer bytes. Choice and evidence: [ADR 0006](../decisions/adr-0006-moving-large-images-through-nifi.md), option O2. This page lists the processors and the properties that matter; it assumes you know the canvas from [nifi-by-hand.md](nifi-by-hand.md). The push step on the high side is [nifi-by-hand-o4-push.md](nifi-by-hand-o4-push.md). Hosts are placeholders: source `registry.example.com`, target `registry-high.example.com`.

## Parameters

Parameter context `registry-layers`: `src_user`, sensitive `src_password`, `dst_user`, sensitive `dst_password`. A registry with a token endpoint (Quay, Docker Hub style) refuses basic auth on `/v2/` and wants a Bearer token from `GET /v2/auth?service=<host>&scope=repository:<repo>:pull[,push]` with basic credentials. A `POST` to that endpoint answers 405, so NiFi's OAuth2 token provider cannot be used: mint the token with `InvokeHTTP`.

## Low side

1. **ListenHTTP** `request`: Base Path `request`, a Listening Port, HTTP Headers to receive as Attributes `X-.*`. Body `{"image":"team/app:1.0","platform":"linux/amd64"}`. Protect the port with mTLS as in [nifi-by-hand-o3-loopback.md](nifi-by-hand-o3-loopback.md).
2. **EvaluateJsonPath** `parse`: Destination `flowfile-attribute`, one property per field (`img.repo`, `img.tag`, `platform`).
3. **InvokeHTTP** `token`: HTTP Method `GET`, HTTP URL `https://registry.example.com/v2/auth?service=registry.example.com&scope=repository:${img.repo}:pull`, Request Username `#{src_user}`, Request Password `#{src_password}`, Response Body Attribute Name `auth.json`, Socket Read Timeout `10 min`. Use `Authorization: Bearer ${auth.json:jsonPath('$.token')}` in the next InvokeHTTP as a dynamic property named `Authorization`.
4. **InvokeHTTP** `manifest GET`: GET `https://registry.example.com/v2/${img.repo}/manifests/${img.tag}`, dynamic properties `Authorization` as above and `Accept` set to the OCI and Docker manifest and index media types, comma separated. Do not parse or re-serialise the response: the raw bytes are the manifest and their sha256 is its digest.
5. **CryptographicHashContent** `digest` (SHA-256) then **UpdateAttribute** `check`: `check` = first 8 hex characters of that digest. This is the check code used by recovery.
6. For an **index** (the response has `manifests`): **SplitJson** (`$.manifests`), **EvaluateJsonPath** (child digest, platform), **UpdateAttribute**, **RouteOnAttribute** on the wanted platform, then loop to step 4 with the child digest as the reference. Send each child manifest as its own FlowFile with `kind=manifest` and `ref=<child digest>`, and the index last with `ref=<tag>`.
7. For an **image** manifest: **SplitJson** (`$..[?(@.digest)]`, which yields the config and every layer), **EvaluateJsonPath** (`blob.digest`), **UpdateAttribute** setting `kind=blob`, `layer.n` and `layer.m` (layer N of M) and `img.name`, `check`.
8. **InvokeHTTP** `blob GET`: GET `https://registry.example.com/v2/${img.repo}/blobs/${blob.digest}`, Socket Read Timeout and Socket Connect Timeout raised to `10 min` (the default 15 s is too short), Response Body Attribute Name empty. The response becomes the FlowFile content. Wire `No Retry` (4xx) and `Retry` (5xx) to a retry or hold queue, never auto-terminate them: a lost blob would otherwise vanish silently.
9. **UpdateAttribute** `strip`: Delete Attributes Expression `auth\.json` so the token does not cross the diode or stay in later provenance events.
10. Leave the connection to the crossing at Load Balance Strategy `Round robin` if you want the high side to spread work. Prioritizer `NewestFlowFileFirstPrioritizer` lets small manifests overtake large blobs.

## High side

1. **RouteOnAttribute** on `kind`: `blob` and `manifest`.
2. **InvokeHTTP** `token` for the target (scope `repository:<repo>:pull,push`), kept only as long as the group needs it.
3. **InvokeHTTP** `HEAD` of `https://registry-high.example.com/v2/${img.repo}/blobs/${blob.digest}`: a 200 means the blob is already there, so route that FlowFile to auto-terminate (no upload, no copy). A 404 goes on.
4. **InvokeHTTP** `POST` of `https://registry-high.example.com/v2/${img.repo}/blobs/uploads/?digest=${blob.digest}`: HTTP Method `POST`, Content-Type `application/octet-stream`, Send Message Body `true`, Socket Write Timeout `10 min`. A monolithic POST streams the content in one request and answers 201. Quay and zot accept it; `registry:2` does not (it fails with `Broken pipe`). For a portable form, use the POST then PUT upload of [nifi-by-hand-native-layer-flow.md](nifi-by-hand-native-layer-flow.md).
5. **Manifest gate** (ExecuteStreamCommand running [manifest_gate.py](../../python/scripts/manifest_gate.py), which needs `push_from_stdin.py` beside it). The FlowFile content is the raw manifest or index. Command Path `python3`, Command Arguments `/opt/nifi/scripts/manifest_gate.py;https://registry-high.example.com;${img.repo};${ref};25;${platform}`, Argument Delimiter `;`, Ignore STDIN `false`, Output Destination Attribute `gate.result`, dynamic `REGISTRY_USER` and sensitive `REGISTRY_PASSWORD` from the parameter context. It polls `HEAD` for the config and every layer (for an index, every child manifest) for up to 25 s, then PUTs the stdin bytes unchanged with the document's own `mediaType`. Exit `0` pushed. Exit `5` still incomplete, with `execution.error` such as `team/app:1.0 (linux/amd64) incomplete: missing layers 2 and 3 of 4. Check code 677a46bf` or `missing platform manifests 1 of 2`. The check code is the first 8 hex characters of the sha256 of the manifest bytes.
6. **RouteOnAttribute** on `execution.status` (`5` goes on, `0` is done, anything else to a hold queue), **LogMessage** (Log message `${execution.error}`, Log Level `warn`) to raise the bulletin, **ControlRate** (30 s) back to the gate.

## Evidence for the gate

Proven live against Quay by the lead: a complete manifest exited 0 and `skopeo inspect --raw` was byte-identical; a manifest whose layers 2 and 3 pointed at absent blobs exited 5 with `incomplete: missing layers 2 and 3 of 4. Check code 33e4393a`, which equals the first 8 hex characters of the stdin sha256.

## Recovery of a lost layer

1. High side: read the `incomplete` bulletin. Note the image, platform, layer numbers and check code.
2. Low side: send `{"image":"team/app:1.0","platform":"linux/amd64","layers":[2,3],"check":"677a46bf"}` to the `request` ListenHTTP. The flow re-resolves the tag, recomputes the check code and refuses on a difference (the tag has moved; ask for a fresh full export). Otherwise it sends only those layers and the config, not the manifest.
3. Wait for the next 30 s re-check. The bulletin stops and the tag appears.

## Check it

- `skopeo inspect --raw docker://registry-high.example.com/team/app:1.0 > high.json` and the same against the source, then `cmp`: the files are byte-identical.
- A second run of the same image drops every blob at the `HEAD` step with no upload.

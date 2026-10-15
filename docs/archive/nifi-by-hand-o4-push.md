# Building the O4 push flow by hand: ExecuteStreamCommand from stdin

> **Archived.** This page describes the O4 push flow, a design the project did not adopt. It is kept for reference. The supported way to build the transfer is [the image transfer runbook](../runbooks/image-transfer-build-by-hand.md).

Pushes a blob or a manifest FlowFile into a registry without Python holding the bytes. Choice and evidence: [ADR 0006](../decisions/adr-0006-moving-large-images-through-nifi.md), option O4. The script is [python/scripts/push_from_stdin.py](../../python/scripts/push_from_stdin.py).

```text
RouteOnAttribute (H1) -> UpdateAttribute (O1) -> ExecuteStreamCommand (O2)
  -> RouteOnAttribute (O3) -pushed-> auto-terminate
                           -unmatched-> RetryFlowFile (O3b) -retry-> back to O2
                                                            -retries_exceeded, failure-> hold queue
```

Hosts are placeholders: the registry is `https://registry.example.com`. FlowFiles reaching H1 carry the content of one blob or one manifest and these attributes: `kind` (`blob` or `manifest`), `dst.repo` (such as `team/app`), `blob.digest` (for a blob) and `ref` (a tag or digest, for a manifest).

## 1. Install the script on every node

Put `push_from_stdin.py` at the same path on every node, for example `/opt/nifi/scripts/push_from_stdin.py` on a ReadWriteMany mount. `python3` (3.10 or later) must be on the NiFi user's PATH. Editing the script needs no NiFi restart: ExecuteStreamCommand starts a new process each time.

## 2. Parameter context

1. **Parameter Contexts** (hamburger menu) > **+**. Name it `registry-push`.
2. Add `registry_user` (not sensitive) with the registry user.
3. Add `registry_password` and tick **Sensitive Value**. Paste the password. **Apply**.
4. Right-click the process group > **Configure** > **Process Group Parameter Context** `registry-push` > **Apply**.

## 3. H1 RouteOnAttribute

Add processor `RouteOnAttribute`, name `H1 route kind`. Properties:

| Property | Value |
|---|---|
| Routing Strategy | Route to Property name |
| blob | `${kind:equals('blob')}` |
| manifest | `${kind:equals('manifest')}` |

Both relationships go to O1. Auto-terminate nothing: an unmatched FlowFile (`unmatched`) goes to the hold queue of section 7.

## 4. O1 UpdateAttribute

Name `O1 push ref`. One dynamic property:

| Property | Value |
|---|---|
| push.ref | `${kind:equals('blob'):ifElse(${blob.digest},${ref})}` |

## 5. O2 ExecuteStreamCommand

Name `O2 push`. Properties:

| Property | Value |
|---|---|
| Command Path | `python3` |
| Command Arguments | `/opt/nifi/scripts/push_from_stdin.py;${kind};https://registry.example.com;${dst.repo};${push.ref}` |
| Argument Delimiter | `;` |
| Ignore STDIN | `false` |
| Working Directory | empty |
| Output Destination Attribute | `push.result` |
| Max Attribute Length | `256` |
| Output MIME Type | empty |

Dynamic properties (Properties tab, **+**):

| Name | Value | Sensitive |
|---|---|---|
| REGISTRY_USER | `#{registry_user}` | no |
| REGISTRY_PASSWORD | `#{registry_password}` | yes (tick **Sensitive Value** in the add dialog) |

Relationships: with Output Destination Attribute set, `original` is the only relationship, so success and failure both arrive on it and `execution.status` tells them apart. Connect `original` to O3. The script writes one JSON line to stdout on success, kept in `push.result`, and the reason to stderr, kept in `execution.error`.

## 6. O3 RouteOnAttribute

Name `O3 pushed`. Routing Strategy `Route to Property name`. Property:

| Property | Value |
|---|---|
| pushed | `${execution.status:equals('0')}` |

Auto-terminate `pushed`: this drops the FlowFile once the registry holds the bytes. `unmatched` goes to O3b.

## 7. O3b RetryFlowFile and the hold queue

Name `O3b retry`. Properties:

| Property | Value |
|---|---|
| Retry Attribute | `flowfile.retries` |
| Maximum Retries | `5` |
| Penalize Retries | `true` |
| Fail on Non-numerical Overwrite | `false` |

Set **Penalty Duration** to `10 sec` on the Settings tab. Connections:

- `retry` goes back to O2.
- `retries_exceeded` and `failure` go to a hold queue: a `LogMessage` named `Hold` that you leave disabled (Log Level `warn`, Log Prefix `Hold: `, Log Message `discarded ${dst.repo}@${blob.digest}: ${execution.error}`, `success` auto-terminated), so enabling it logs each FlowFile as it discards it. Content is never auto-terminated on failure. Connect H1 `unmatched` to the same queue.

## 8. Facts that shape the settings

- **Exit codes.** `0` pushed, `10` credentials refused, `11` upload start refused, `12` transfer failed, `13` commit refused (the bytes do not match the digest), `14` manifest refused, `15` bad arguments or network error. They arrive in `execution.status`; the text is in `execution.error` (capped at 4000 bytes).
- **Manifest before its blobs.** The registry answers 400 for a manifest whose blobs are missing (exit 14). The penalised retry pushes it later. For multi-GB blobs raise Maximum Retries or the penalty so the manifest outlasts the slowest blob.
- **A kill mid-push** leaves the FlowFile intact (exit 137) and a dangling upload in the registry. The script prints `upload=<id>` to stderr. The next push of the same blob starts a new upload and replaces the dangling one.
- **Secrets.** `REGISTRY_PASSWORD` is masked in the NiFi UI and REST, and absent from argv and default `ps`. It is readable in `/proc/<pid>/environ` and `ps eww` by the same uid and root. Use a registry robot account scoped to the target repositories.
- **Digest.** A blob push sends the declared digest with the commit; the registry rejects a mismatch (exit 13). A manifest is sent with its bytes unchanged, so its digest does not move.
- **Same path on every node.** The script path must exist on every node, or a FlowFile that runs on a node without it fails (python3 exits 2) and is retried until it lands on a node that has it or goes to the hold queue.

## 9. Check it

1. Start the group and queue one small blob FlowFile with `kind=blob`, `dst.repo=team/app`, `blob.digest=sha256:<its sha256>`.
2. It leaves the O3 `pushed` connection. In **Data provenance** on O2, open the event and see `execution.status` `0` and `push.result` with the byte count.
3. `curl -I https://registry.example.com/v2/team/app/blobs/sha256:<digest>` answers 200 with the right `Content-Length`.
4. Set `blob.digest` to a wrong value. Expect `execution.status` `13` and the FlowFile in the hold queue after 5 retries.

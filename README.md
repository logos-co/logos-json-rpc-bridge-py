# logos-bridge

A Python client for [logos-json-rpc-bridge](https://github.com/logos-co/logos-json-rpc-bridge),
the Logos module that exposes loaded modules' methods and events over JSON-RPC 2.0 on
HTTP and WebSocket.

- `AsyncBridgeClient`: asyncio client over WebSocket (calls, subscriptions, bridge ops).
- `BridgeClient`: the same API, blocking, on a background event loop.
- `BridgeHttpClient`: standard-library HTTP client for `POST /rpc` and the read-only routes.
- `logos-bridge-codegen`: typed Python clients (and Markdown reference pages) generated
  from a module's LIDL contract.
- `bridge.module(name)`: a typed proxy built at run time from the contract the bridge serves.
- `logos_bridge.lidl`: the LIDL contract model, validation and digests.
- `logos_bridge.testing`: `FakeBridge`, a protocol-faithful fake bridge; `FakeProvider`, a
  fake module that checks calls against its contract; and `async_test`, a pytest-free
  runner for async tests.
- `logos_bridge.testing.live`: a real bridge under a `logoscore` daemon, for integration
  tests (see [Testing against a real bridge](#testing-against-a-real-bridge)).

The package connects to a bridge that is already running. It never starts `logoscore`.
Its only runtime dependency is `websockets>=15.0`. Reading `.lidl` files and `.lgx`
packages needs the `lidl` and `lgx` command-line tools (see [Typed clients](#typed-clients)).

> Status: 0.1.0, the generic client plus typed clients from LIDL contracts.

## Install

```bash
pip install logos-bridge
```

With Nix: `nix build github:logos-co/logos-json-rpc-bridge-py` (package `logos-bridge`).

## Quickstart (asyncio)

```python
import asyncio
from logos_bridge import AsyncBridgeClient

async def main():
    async with AsyncBridgeClient("ws://127.0.0.1:8645/ws") as bridge:
        print(await bridge.call("provider_module", "greet", "world"))   # "hello, world"

        async with bridge.subscribe("provider_module", "tick") as ticks:
            await bridge.call("provider_module", "fire", 42)
            event = await ticks.get(timeout=5)
            print(event.data, event.generation)                           # [42] 1

        for info in await bridge.list_modules():
            print(info.module, info.methods, info.events)

asyncio.run(main())
```

`subscribe()` returns an object you can `await` (then call `unsubscribe()` yourself) or
use with `async with`. `subscribe_many(module, [events])` gives one stream that keeps the
arrival order across events.

## Quickstart (blocking)

```python
from logos_bridge import BridgeClient

with BridgeClient() as bridge:
    print(bridge.call("provider_module", "greet", "world"))

    handle = bridge.on_event("provider_module", "tick", lambda event: print(event.data))
    bridge.call("provider_module", "fire", 1)
    ...
    handle.cancel()          # no callback runs after this returns

    with bridge.subscribe("provider_module", "tick") as ticks:
        for event in ticks:  # blocks; ends after unsubscribe
            ...
```

Callbacks run on the portal's dispatch thread, never on its event loop, so a callback may
call `bridge.call(...)`. Clients can share one loop: `BridgeClient(portal=my_portal)`.
Blocking calls from inside a running event loop raise `BlockingCallInEventLoop`.

Over HTTP (no subscriptions):

```python
from logos_bridge import BridgeHttpClient

http = BridgeHttpClient("http://127.0.0.1:8645")
print(http.healthz().status, http.call("provider_module", "greet", "http"))
```

## Bringing up a bridge

The bridge is an ordinary module. Load it next to the modules it should expose, then start
it with a config naming them (see the bridge's README and its doc-test,
`doctests/json-rpc-bridge.test.yaml`):

```bash
logoscore -D -m ./modules &
logoscore load-module provider_module
logoscore load-module json_rpc_bridge
logoscore call json_rpc_bridge start \
  '{"http":{"port":8645},"expose":{"modules":["provider_module"]}}'
curl -s http://127.0.0.1:8645/healthz
```

With `logosctl` (the binary releases ship), the config is a plain string argument:

```bash
echo '{"http":{"port":8645},"expose":{"modules":["provider_module"]}}' > bridge.json
logosctl daemon start --detach
logosctl module load provider_module
logosctl module load json_rpc_bridge
logosctl call json_rpc_bridge start @bridge.json   # @file passes the file's text
```

The bridge binds loopback only and has no wildcard: every reachable module is listed in
`expose.modules`. `json_rpc_bridge.getInfo()` reports the bound endpoints.

To develop against a bridge with typed test modules, `nix run .#dev-bridge` does all of the
above (see [Development](#a-bridge-to-develop-against)).

## Contracts and discovery

A bridge that discovers modules through `lidl()` describes each exposed module with an
`interface_status`, in `rpc.schema` (`await bridge.schema(m)`) and `rpc.list_modules`:

| Status | Meaning |
|---|---|
| `pending` | discovery has not finished yet; `await bridge.wait_for_module(m)` waits it out |
| `ok` | `lidl()` returned a contract that parses and validates; `source` is `"lidl"` |
| `untyped` | the module has no usable `lidl()` (an older build); the names come from `getPluginInterface` |
| `invalid` | `lidl()` returned text that does not parse or validate; `interface_error` says why |
| `None` | the bridge predates `lidl()` discovery |

`ModuleInfo` also carries `exposure` (the members this bridge's policy permits; for an
`ok` module it always includes `name`, `version` and `lidl`), `interface_sha256`,
`contract_sha256`, `cross_check` (the contract compared with the module's live report),
and `stale` (the bridge has seen a change and is discovering the module again). Calling a
denied member fails exactly like calling an unknown one (-32601).

`lidl()` and `rpc.schema` answer different questions:

- `lidl()` is a method of the module. It returns the contract text the module was built
  with, and `sha256(text)` is `contract_sha256`. Call it like any other method, for example
  `await bridge.call(m, "lidl")`.
- `rpc.schema` is the bridge's view. For an `ok` module, `interface` is the parsed
  contract as a JSON AST (with `name`, `version` and `lidl` added), and `interface_sha256`
  is the SHA-256 of its canonical JSON. `rpc.list_modules` returns the same views without
  `interface`.

`logos_bridge.lidl.Interface` reads either form. `logos_bridge.lidl_sources.interface_from_module_info(info)`
builds it from a view, and `logos_bridge.digest` computes all three digests
(`interface_sha256`, `contract_sha256`, `shape_sha256`).

**When a module changes.** The bridge re-reads each module's live report every
`discovery.revalidate_ms` (10 s by default). If the module is a different build, the view
becomes `stale`, discovery runs again (the status and digests may change), and every
subscription to the module ends with `SubscriptionTerminated` (`reason:
"provider_changed"`). A typed consumer should run `check_compat()` again before it
subscribes again. A provider that the protocol loses ends subscriptions with
`provider_unavailable` instead. A quick reload of the same build could go unnoticed on a
bridge whose logos-protocol predates `dcf4f05`: events kept flowing and
`Event.generation` did not change. logos-protocol#91, which `dcf4f05` carries and the
locked bridge `efd4721` therefore has, reports such a swap on the qt_remote transport —
the subscription ends and the next one starts at a higher generation.

## Typed clients

`logos-bridge-codegen` writes a typed client module for one contract:

```bash
logos-bridge-codegen python --lidl storage_module.lidl -o storage_client.py
logos-bridge-codegen python --lgx storage_module.lgx --module storage_module -o storage_client.py
logos-bridge-codegen python --from-bridge ws://127.0.0.1:8645/ws --module storage_module -o storage_client.py
logos-bridge-codegen python --lidl storage_module.lidl -o storage_client.py --check   # exit 1 on drift
logos-bridge-codegen markdown --lidl storage_module.lidl -o storage_module.md
logos-bridge-codegen digest --lidl storage_module.lidl            # or --shape, --contract
```

**Sources** (exactly one):

- `--lidl FILE`: parsed by `lidl json --identity`. A file that differs from `lidl fmt`
  output gets a note.
- `--ast FILE`: a JSON AST (`lidl json` output). `name`, `version` and `lidl` are added
  unless `--no-identity` is given.
- `--lgx FILE --module M`: runs `lgx verify`, reads the manifest, extracts the assets
  (`--assets-only` when the `lgx` supports it, otherwise one variant), and reads
  `assets/lidl/M.lidl`. A package also carries its dependencies' contracts, so `--module`
  is required. Packages are opened only through `lgx`.
- `--from-bridge URL --module M`: waits out `pending`, requires status `ok`, calls
  `M.lidl()`, checks the text against `contract_sha256`, and parses it with `lidl`. Without
  a `lidl` CLI it uses the served `interface`. Through a tunnel, add `--host-header`.

The CLIs are found through `--lidl-cli`/`--lgx-cli`, then `LOGOS_LIDL_CLI`/`LOGOS_LGX_CLI`,
then `PATH`. With Nix they are `logos-lidl#lidl-cli` and `logos-package#lgx`.

| Exit | Meaning |
|---|---|
| 0 | written, or `--check` found no difference |
| 1 | `--check` found a difference (a diff is printed) |
| 2 | usage error, for example an unused `--rename` |
| 3 | the source is unavailable: a missing file or CLI, a bridge that cannot be reached, or a module that is not `ok` |
| 4 | the contract is rejected: parse or validation errors, a failed `lgx verify`, types this version cannot map, or Python name collisions |
| 5 | the output cannot be written |
| 70 | internal error |

**Names.** Methods and events become snake_case (`echoBlob` becomes `echo_blob`, and
`blobEvent` becomes `on_blob_event()`). Records and event classes are PascalCase
(`BlobEventEvent`). A keyword or reserved name gets a trailing `_`, and a name that is not
an identifier gets a `lidl_` prefix. A collision stops generation with exit 4 and names
the fix: `--rename KIND:NAME=PYTHON_NAME`, where KIND is `method`, `event`, `type`, `field`
(`Type.field`), `param` (`method.param`) or `eparam` (`event.param`).

**The generated module** (for a module `storage_module`):

- `AsyncStorageModuleClient(bridge, module=MODULE_NAME)` over an `AsyncBridgeClient`, and
  `StorageModuleClient(bridge)`, its blocking twin over a `BridgeClient` (with `.aio` and
  `.portal`).
- One method per LIDL method, including `name()`, `version()` and `lidl()`. Each takes a
  keyword-only `timeout`.
- `on_<event>()` returns a typed subscription (use it with `async with`, or `await` it).
  `events(*names)` returns one ordered stream of several events, and `decode_<event>(event)`
  decodes a raw `Event`.
- `check_compat(allow_untyped=False, discovery_wait=10.0)` returns a `CompatReport`.
- Records are frozen, slotted, keyword-only dataclasses. Each event has a
  `<Pascal>Event` dataclass with its parameters plus `meta` (the raw `Event`). The module
  also defines a `StorageModuleEvent` union, a `StorageModuleEventName` literal, and
  `EVENT_NAMES`.
- `INTERFACE` (the contract in shape form), `INTERFACE_SHA256`, `CONTRACT_SHA256`,
  `SHAPE_SHA256`, `MODULE_NAME`, `RECORD_TYPES` and `EVENT_TYPES`.

```python
from logos_bridge import AsyncBridgeClient
from storage_client import AsyncStorageModuleClient

async with AsyncBridgeClient(url) as bridge:
    storage = AsyncStorageModuleClient(bridge)
    report = await storage.check_compat()           # raises unless compatible
    async with storage.on_storage_upload_done() as done:
        started = await storage.upload_url("/data/file.bin", 65536)   # a LogosResult
        started.unwrap()                            # ModuleResultError if success is false
        event = await done.get(timeout=60)          # a StorageUploadDoneEvent
        print(event.payload, event.meta.generation)
```

At import, the module checks `INTERFACE` against `SHAPE_SHA256` (`BindingIntegrityError`
if it was edited) and that the installed `logos-bridge` supports its codegen format
(`CodegenFormatError`). The header records the contract, the three digests, the codegen
format and how to regenerate (`--regen-hint`). The `# Source:` and `# Generator:` lines
are provenance: `--check` ignores them unless `--strict-provenance` is given. The output
is deterministic, with the same bytes on every supported Python.

**Types.**

| LIDL | Argument | Result, record field, event value |
|---|---|---|
| `tstr` | `str` | `str` |
| `bstr` | `bytes` | `bytes` |
| `int`, `uint` | `int` (checked against the 64-bit range) | `int` |
| `float64` | `float` | `float` |
| `bool` | `bool` | `bool` |
| `any` | `Any` (`bytes` inside become `_bytes` tags) | `Any`, with `_bytes` tags left as they are |
| `[T]` | `Sequence[T]` | `list[T]` |
| `{tstr: V}` | `Mapping[str, V]` | `dict[str, V]` |
| `? T` | `T \| None` | `T \| None` |
| a record | its dataclass | its dataclass |
| `result` | `LogosResult[Any]` | `LogosResult[Any]` (`.unwrap()` raises `ModuleResultError`) |
| no return | | `None` |

Arguments are checked and encoded before anything is sent. A problem raises
`ArgumentError` (or `ArityError`) with the argument's path, and no request is sent. An
omitted trailing optional argument is sent as `null`, and an unset optional record field
is left out. Generated clients call `rpc.call` only, never the bridge's
`<module>.<method>` aliases. A provider refusal always raises `ProviderRejection`. When
a legitimate result could look like a refusal (`any`, or a map whose values can be
strings), the generator prints a note. A result that does not match raises `ResultDecodeError`. An event that does
not match raises `EventDecodeError` from `get()` and iteration, while
`subscription.results()` yields the error and continues. `SubscriptionTerminated` ends a
typed stream, whatever the reason.

**Compatibility.** `check_compat()` waits up to `discovery_wait` seconds for a `pending`
module (then raises `DiscoveryPending`) and compares the served contract with the
client's:

| Level | When |
|---|---|
| `exact` | `interface_sha256` is equal |
| `shape` | same module name, and every member the client knows is served with the same signature; descriptions, the version and extra members may differ |
| `structural` | as `shape`, except that record and parameter names may differ |
| `names` | the module has no valid contract and `allow_untyped=True`; only the live names are compared |

When no level applies, it raises `IncompatibleModule`, or `UntypedModule` if the module is
not `ok` and `allow_untyped` is false. The report lists `missing`, `mismatched`, `extra`
and `not_exposed` members (the last are declared but hidden by the bridge's policy).
`report.require(methods=[...], events=[...])` raises unless the members you use are all
served, matching and exposed. Run `check_compat()` again after `provider_changed`.

## The dynamic proxy

Without generated code, `bridge.module(name)` builds a proxy from what the bridge serves:

```python
storage = await bridge.module("storage_module", require_typed=True)
print(storage.interface_status, storage.is_typed, storage.exposed_methods)
async with storage.on("storageUploadDone") as done:
    started = await storage.uploadUrl("/data/file.bin", 65536)   # or storage["uploadUrl"](...)
    event = await done.get(timeout=60)      # a DecodedEvent: .args, .values["payload"], .meta
```

Members keep their LIDL names. For an `ok` module, calls are checked and encoded
locally, and results and events are decoded (records as dicts). Each method has an
`inspect.signature` built from the contract. A parameter named `timeout`, or named after
a keyword, gets a trailing `_`. A declared method that the bridge does not expose raises
`MethodNotExposed` without a request. A type this version cannot map is treated as `any`. For `untyped` and `invalid`
modules, and for bridges without `lidl()` discovery, the proxy makes plain `rpc.call`
requests and emits a `BridgeWarning`. `require_typed=True` raises `UntypedModule` instead.
After `provider_changed`, `storage = await storage.refreshed()` rereads the contract.
`BridgeClient.module()` returns the blocking twin.

**Names.** Attributes belong to the module, so `storage.name()`, `storage.version()` and
`storage.lidl()` always call its built-ins. The proxy's own API uses names a module is
unlikely to declare: `module_name`, `module_info`, `interface_status`, `is_typed`,
`untyped_reason`, `served_interface`, `typed_plans`, `bridge_client`, `exposed_methods`,
`exposed_events`, `declared_methods`, `declared_events`, `decode_event`, `refreshed` and
`from_module_info`, plus `call`, `on` and `events` (the blocking twin adds `aio` and
`portal`). A method named like one of these, or starting with `_`, is reached by key:
`await storage["events"]()`.

## Values

- Arguments are positional. `bytes`, `bytearray` and `memoryview` travel as
  `{"_bytes": "<base64url, unpadded>"}`; tuples become arrays. A map key named `_bytes` is
  reserved, and a pre-encoded tag must be unpadded base64url.
- `call(..., decode_bytes=True)` turns such tags in the result back into `bytes`. Event
  payloads (`Event.data`) are delivered verbatim; `event.decoded_data()` decodes them.
- JSON is strict: no NaN or Infinity, `str` keys only, valid Unicode only. 64-bit integers
  are exact.
- A method returning `{"success": false, "value": ..., "error": ...}` *answered*: the
  result is returned, not raised. `unwrap_result(result)` returns `value` or raises
  `ModuleResultError`.
- A provider that refuses a call answers `{"code", "message", "origin"}` as its result.
  `call()` raises `ProviderRejection` for the codes `dispatch_failed`, `invalid_args` and
  `unknown_method`, exactly as the Rust SDK's `as_dispatch_rejection` does. Pass
  `detect_rejection=False` to get the object instead.

## Errors

Every exception derives from `BridgeError`.

| Error | When |
|---|---|
| `MethodNotFound` (-32601) | unknown, unexposed or denied module/method; deliberately indistinguishable |
| `InvalidParams` (-32602) | bad params; `.detail` holds `reason` and `path` |
| `ParseError`, `InvalidRequest` | the bridge could not read a request |
| `UpstreamCallFailed` (-32603), `CallNotDispatched` (-32000) | the call could not run |
| `ModuleUnavailable` (-32001) | the provider is not up |
| `UpstreamTimeout` (-32002) | the bridge's own deadline (`limits.call_timeout_ms`) passed |
| `UpstreamTransportError` (-32003), `NotAuthorised` (-32004) | transport or policy failure upstream |
| `Cancelled` (-32005), `ShuttingDown` (-32006), `Overloaded` (-32029) | reserved; bridge draining; batch or subscription limit |
| `ProviderRejection` | the provider refused the arguments (a result, see above) |
| `MethodNotExposed` | a typed call to a declared member this bridge does not expose (a `MethodNotFound`; nothing is sent) |
| `ArgumentError`, `ArityError` | a typed call's arguments do not match the contract (a `TypeError`; nothing is sent) |
| `ResultDecodeError`, `EventDecodeError` | a result or event does not match the contract (a `ValueError`, with `.path`) |
| `IncompatibleModule`, `UntypedModule` | `check_compat()` found no usable level (`.report`) |
| `DiscoveryPending` | the module stayed `pending` longer than the wait (a `ClientTimeout`) |
| `CodegenFormatError`, `BindingIntegrityError` | a generated module needs a newer runtime, or was edited |
| `ClientTimeout` | this client's deadline passed (a `TimeoutError`) |
| `ConnectError` | the connection could not be opened; `.hints` lists likely causes |
| `ConnectionClosed` | the connection ended (a `ConnectionError`), raised fresh to every waiter |
| `SubscriptionTerminated` | the bridge ended a subscription (`reason`: `provider_unavailable` or `provider_changed`) |
| `SubscriptionOverflow` | more than `max_pending` events queued |
| `HttpStatusError` | a non-200 HTTP answer (403 Host/Origin, 401 bearer, 415 content type, 404) |
| `RequestTooLarge`, `BytesDecodeError`, `PortalStopped`, `BlockingCallInEventLoop` | local misuse |

RPC errors carry `code`, `message`, `data`, `logos_error_code` and `logos_error_name`.
An error the bridge sends with `"id": null` cannot be matched to a request over
WebSocket: it is kept in `client.last_uncorrelated_error`, emitted as a `BridgeWarning`,
and attached to the next close as `ConnectionClosed.server_error`. Over HTTP it is raised.

`ConnectionClosed` has `code`, `code_name`, `reason`, `initiated_by`
(`server`, `client` or `transport`) and a `hint`:

| Code | Meaning with this bridge |
|---|---|
| 1000 | normal close |
| 1001 | the peer is going away |
| 1003 | a binary frame was sent; the bridge takes text only |
| 1006 | no close frame: the bridge stopped or unloaded, its process died, or the network failed |
| 1007 | invalid UTF-8 |
| 1008 | slow reader: more than `limits.max_queued_frames_per_connection` (256) frames were queued |
| 1009 | a request over `limits.max_frame_bytes` (1 MiB); keep `max_request_size` at or below it |
| 1011 | sent by this client when a keepalive ping got no pong in time |

A refused WebSocket upgrade is dropped without an HTTP answer, so `ConnectError.hints`
lists the possible reasons: a Host that is not a loopback literal, an Origin header, the
per-peer cap of 8 connections (bridges without the keep-alive fix leak one slot per
kept-alive HTTP request), `max_connections`, `auth.mode: bearer`, or a different
subprotocol.

## Subscriptions and flow control

The bridge cannot slow a producer down, so it closes a connection whose outbound queue
overflows (1008). This client therefore never stops reading: one reader task moves every
event into an unbounded per-subscription queue, and `pending`/`high_water` show how far
behind you are. A `BridgeWarning` fires at 10,000 queued events. Pass `max_pending=N` to
drop the subscription (after draining) with `SubscriptionOverflow` instead of growing.

Subscription ids are generated per attempt (`s<hex>-<n>`) and registered before the
request is sent, so events that arrive before the acknowledgement are kept. A failed
subscribe never reuses its id. When the bridge terminates a subscription, queued events
are delivered first, then `SubscriptionTerminated` is raised; the same holds for a
disconnect (`ConnectionClosed`). Subscribe again for a fresh stream and treat the gap as
lost; `Event.generation` increases after a provider restart. From logos-protocol
`dcf4f05` on it counts provider establishments rather than subscribers, so a second
subscriber joining a live provider sees the generation it already had. After
`provider_changed` the module is a different build: check its contract again first (see
[Contracts and discovery](#contracts-and-discovery)).

## Timeouts

- `call_timeout` (default `None`) bounds `call()`; `op_timeout` (30 s) bounds everything
  else; `timeout=` overrides per call, `timeout=math.inf` disables it.
- A `ClientTimeout` only stops waiting. The bridge has no way to cancel the upstream call
  (`rpc.cancel` answers `not_supported_upstream`), so the call may still run and take
  effect. Module calls are not idempotent: do not retry blindly.
- `UpstreamTimeout` is the bridge's own deadline and is deliberately not a `TimeoutError`.
- `ping_interval`/`ping_timeout` (20 s each) detect a stalled bridge.

## No reconnect

A client is one connection. After a disconnect every pending call fails and every
subscription ends, because a new connection cannot resume them: subscription state is
per connection, events in between are lost, and in-flight calls may or may not have run.
Recovering correctly is application-specific, so this version leaves it to you:
`await client.wait_closed()`, inspect `client.close_exception`, create a new client,
re-subscribe and refetch state.

## SSH tunnels and port forwards

The bridge accepts only `Host: 127.0.0.1`, `localhost` or `[::1]` (optionally with its own
port). Through a tunnel the port differs, so pass the Host the bridge expects:

```python
# ssh -L 18645:127.0.0.1:8645 node
AsyncBridgeClient("ws://127.0.0.1:18645/ws", host_header="127.0.0.1:8645")
BridgeHttpClient("http://127.0.0.1:18645", host_header="127.0.0.1:8645")
```

Environment proxies are ignored, no Origin header is sent, and `Host` cannot be set
through `extra_headers`.

## Testing with FakeBridge

```python
from logos_bridge import AsyncBridgeClient
from logos_bridge.testing import Delay, FakeBridge, FakeError, NoResponse, Reject, async_test

@async_test(timeout=10)
async def test_greeting():
    async with FakeBridge() as fake:
        fake.module("m", [("greet", ["who"])], ["tick"])
        fake.on_call("m", "greet", lambda ctx: f"hello, {ctx.params[0]}")
        async with AsyncBridgeClient(fake.url) as client:
            assert await client.call("m", "greet", "you") == "hello, you"
            assert (await fake.wait_for_request("rpc.call")).params["method"] == "greet"
```

Handlers return a value, `FakeError(code)`, `Reject(code, message, origin)`,
`NoResponse`, `Delay(seconds, then)`, or a (sync or async) callable taking a context whose
`emit()` sends events. The fake reproduces the bridge's result shapes and messages,
null-id errors, the batch cap, duplicate-id and unknown-unsubscribe acks, close codes
1003/1008/1009 (1006 on `stop()`), dropped upgrades, `rpc.schema` views with or without
`lidl()` discovery (`status=`), and optional quirks of older bridges
(`poison_failed_subscribe_ids`, `emit_events_before_subscribe_ack`). It also records
requests, handshakes and HTTP exchanges, and can `freeze()`, `set_draining()`,
`terminate()`, `close_connections()` and `abort_connections()`. `ThreadedFakeBridge` is
the blocking twin, and `http_url` serves the HTTP routes with the bridge's framing rules:
411 for a body it cannot read, and `Connection: close` after an answer that leaves a body
unread. Unlike the bridge, the fake answers pipelined requests, and its refusal pages use
different markup. `async_test` runs each test in a fresh loop with a hard timeout and fails
on leftover tasks; no pytest plugin is needed.

**Contracts in the fake.** `fake.module(name, interface=...)` serves a module the way a
`lidl()`-discovering bridge does. `interface` is an `Interface`, a JSON AST, or a generated
module's `INTERFACE`. `lidl_text=` sets what `lidl()` returns (without an `interface`, the
text is parsed with the `lidl` CLI). `status=` is `ok` (the default with a contract),
`pending`, `untyped` or `invalid` (with `interface_error=`), and each gives the bridge's
view for that status, including both digests and `cross_check`. `exposure=` hides
members: calling a hidden member returns -32601, and `name`, `version` and `lidl` are
always callable. Calling a module with a contract checks by-name parameters (a missing one
returns -32602 with its name as `path`). Reloading a module with a different contract ends
its subscriptions with `provider_changed`; `terminate_on_change=False` models a reload the
bridge does not notice, and `mark_stale(name)` models the time before rediscovery.
`Wire(value)` returns a result exactly as given, and `emit_json()` sends an event payload
as given.

**FakeProvider** implements a module from its contract:

```python
import mini_client                   # generated from mini_module.lidl
from logos_bridge.testing import FakeBridge, FakeProvider

class Notes:
    def put(self, note):             # a Note here; a dict when no records= are given
        return note
    def find(self, id, prefix):
        return {"success": True, "value": id, "error": None}
    def clear(self):
        pass

async with FakeBridge() as fake:
    provider = FakeProvider(mini_client.INTERFACE, Notes(), records=mini_client.RECORD_TYPES)
    provider.install(fake)           # fake.module(...) plus a handler per method
    provider.emit("added", mini_client.Note(id="n1", body=b"hi"))   # encoded per its declaration
```

It checks each call the way a C++ provider does: a wrong argument count returns
`invalid_args`, and a wrong value returns `dispatch_failed`, with the provider's own
wording, as the call's result. It then calls the implementation (a mapping, or an object
with LIDL-named or snake_case methods, sync or async) and encodes the return per the
contract. `name()`, `version()` and `lidl()` answer from the contract.

**More helpers.** `logos_bridge.testing.conformance` reads logos-test-modules' conformance
tables. `logos_bridge.testing.docs` checks a bridge's OpenRPC, OpenAPI and AsyncAPI
documents: it validates captured results, parameters and events against their schemas,
and lists operations for members that are not exposed. Validation needs the `docs-test`
extra (`jsonschema`), but the module imports without it.

## Testing against a real bridge

`logos_bridge.testing.live` runs a real `json_rpc_bridge` under a `logoscore` daemon, for
the integration tests of this package and of SDKs built on it:

```python
from logos_bridge import AsyncBridgeClient
from logos_bridge.testing import async_test
from logos_bridge.testing.live import async_live_bridge, bridge_config

@async_test(timeout=120)
async def test_storage_is_typed() -> None:
    config = bridge_config(["storage_module"], limits={"call_timeout_ms": 60000})
    async with async_live_bridge(["storage_module"], config) as node:
        async with AsyncBridgeClient(node.ws_url) as bridge:
            assert (await bridge.schema("storage_module")).interface_status == "ok"
```

`live_bridge()` (a `with` block) and `async_live_bridge()` (an `async with` block, which
starts and stops the node in a worker thread) do the same:

1. start a daemon over the stack's modules directories;
2. load the providers, then `json_rpc_bridge`;
3. start the bridge with `config` on a free port (or `port=`), by default exposing the
   providers with `discovery.revalidate_ms` 1000;
4. wait until every exposed module has left `pending`, is not stale, and appears as such
   in the served documents, which the bridge rebuilds 25 ms after a change;
5. yield a `LiveBridge`, and on exit stop the daemon and kill any module host that
   outlived it.

| API | |
|---|---|
| `live_bridge(providers=(), config=None, *, stack=None, also_expose=(), port=None, settle=60.0, label="live")` | a started `LiveBridge` for the block (`stack`: `LiveStack.from_env()`; `also_expose`: modules the daemon runs itself, exposed without being loaded) |
| `async_live_bridge(...)` | the same arguments, for `async with` |
| `LiveStack.from_env(env=None, *, modules_dirs=())` | the `logoscore` CLI and the modules directories; `LiveBridgeUnavailable` names what is missing |
| `LiveStack(logoscore, modules_dirs, run_dir=None)` | the same, given directly |
| `bridge_config(modules, *, revalidate_ms=1000, **sections)` | a bridge config: names or `{"name", "methods", "events"}` entries, plus sections such as `limits=` |
| `LiveBridge` | `ws_url`, `http_url`, `port`, `config` (as started), `started` (`start()`'s answer), `exposed`, `config_dir`; `info()` (`getInfo`), `views()`, `view(module)`, `wait_settled(timeout)`, `wait_status(module, status, timeout)`; `load(module)`, `unload(module)`, `reload(module)`, `daemon_call(module, method, *args)`; `start_bridge(config)`, `stop_bridge()`; `logs()`, `stop()`; `daemon` and `client` are logos-logoscore-py's objects |
| `short_tmpdir_env(prefix="lb")` | on macOS, a short `TMPDIR` for the block (below) |
| `interrupt_on_sigterm()` | SIGTERM raises `KeyboardInterrupt`, so pytest's finalizers stop the daemons |
| `python -m logos_bridge.testing.live --reap DIR` | kills every process whose command line names a path under `DIR` |

| Variable | |
|---|---|
| `LOGOS_LOGOSCORE_BIN` | the `logoscore` CLI (then `LOGOSCORE_BIN`, then `PATH`); nix: `github:logos-co/logos-logoscore-cli` |
| `LOGOS_BRIDGE_INSTALL_DIR` | the bridge's installed modules (with `json_rpc_bridge/` in it), or an install output holding `modules/`; nix: `github:logos-co/logos-json-rpc-bridge#install` |
| `LOGOS_LIVE_MODULES_DIRS` | the modules under test, separated by `os.pathsep`, in the same two forms (for example a module's `#install` output) |
| `LOGOS_LIVE_RUN_DIR` | where daemons get their config directories (default: the temporary directory); the reaper's `DIR` |

- **logos-logoscore-py.** The daemon is its `LogoscoreDaemon`, from the `logoscore`
  package. That package is not on PyPI, so it is not a dependency or an extra: install it
  with `pip install git+https://github.com/logos-co/logos-logoscore-py`, or put its `src/`
  on `PYTHONPATH` (a `flake = false` input in nix). It is imported on first use.
- **Teardown.** Module hosts run in process groups of their own and name the daemon's
  config directory on their command lines; that is how the ones that outlive the daemon are
  found. If the test process itself is killed, nothing runs: call the reaper on
  `LOGOS_LIVE_RUN_DIR` from the shell that started the tests (this repository's
  `mkIntegration` does it in a `trap`).
- **`TMPDIR`.** The daemon's local sockets are `$TMPDIR/logos_<module>_<id>`, and macOS
  caps socket paths at 104 bytes, so a long `TMPDIR` (a nix build's, or `/var/folders/…`)
  breaks them. `short_tmpdir_env()` points the whole process at `/tmp/<prefix>.*` for the
  block; every `logoscore` call must see the same `TMPDIR` as the daemon.
- **Calls through the daemon** (`daemon_call`) follow the `logoscore` CLI's argument
  typing: an all-digit string becomes a number.
- **The Linux nix sandbox.** An install tree is unpacked from a compressed `.lgx`, so nix
  does not see the libraries its plugin's RUNPATH names. Make the check depend on the
  module's plugin build too (its `lib` output), as `LOGOS_BRIDGE_PLUGIN_BUILDS` does here.

## Known bridge limitations

- Every call runs with the bridge's own authority; `expose.modules` is a containment
  filter, not per-client authorization. Only `auth.mode: none` works today.
- Loopback only. A subscription over HTTP is acknowledged and then goes nowhere.
- `rpc.cancel` stops nothing upstream.
- A slow WebSocket reader is closed (1008) rather than throttled.
- Event payloads are positional arrays; parameter names come from `rpc.schema`.
- Discovery is the module's unvalidated self-report (`authoritative: false`) unless the
  bridge serves a validated `lidl()` contract (`interface_status: ok`). Even then,
  `authoritative` stays `false`: the contract is what the module claims about itself.
- A changed module is noticed only at the next revalidation (`discovery.revalidate_ms`),
  and a quick reload of the same build may not be noticed at all.
- A provider reloaded while the bridge relayed its stream (a chunked download, say) can
  stay unreachable through the bridge's upstream client. Bridges from
  logos-json-rpc-bridge `c8135ec` (#6) on replace that client, measured at about 5.5 s,
  and count it in `getInfo().upstream_clients_replaced`.
  - An older bridge needs a restart (`json_rpc_bridge.stop`, then `start`), and the
    order matters: **load the provider first, then restart the bridge.** A restart
    while the provider is still down, followed by a subscription, crashed the
    pre-recycler build (`c99bbc5`) in 4 of 4 rounds. The fault is a use-after-free in
    logos-protocol, not in the bridge: `RemoteTransportConnection::requestObject()`
    freed a replica facade that had never reached `Valid`, while Qt Remote Objects
    still held it as a raw pointer. logos-protocol#95 parks such a facade instead of
    freeing it, and a Qt patch for the same hazard is queued separately; neither is in
    the bridge this package locks (`efd4721`, whose logos-protocol `dcf4f05` predates
    #95), so the ordering rule stands there too.
- A call to an exposed module whose provider is not loaded is answered `ModuleUnavailable`
  (-32001) when `limits.call_timeout_ms` runs out (measured: 29.7 s at the default 30 s)
  **or later**, although its subscriptions end at once.
  - Concurrent calls to an unreachable module do not each get their own deadline. Each
    blocks the host's Qt main thread, the next one nests inside it, and none of them can
    time out while new ones keep arriving. Measured: 14 calls, one per second across a
    14 s outage, were all answered only once the provider came back — and as
    `UpstreamTimeout` (-32002), not `ModuleUnavailable`. Send calls to a provider you
    expect to be down one at a time.
- Subscription loss is reported only with logos-protocol 0.9 or newer
  (`subscription_continuity` in `getInfo()`).
- Bridges without the keep-alive fix count every kept-alive HTTP request against the
  per-peer cap of 8; this package's HTTP client always sends `Connection: close`.
- A body over `limits.max_body_bytes` gets no 413: the bridge drops the connection without
  an answer.
- A `HEAD` request gets the `GET` answer, body included.
- `PUT`, `PATCH`, `DELETE` and `OPTIONS` are served as `GET`, and so is every method but
  `POST` by `FakeBridge`.
- HTTP pipelining is broken (libwebsockets 4.3.5): requests sent back to back on one
  connection go unanswered, or the connection is closed. Send a request only after the
  previous answer; this package's HTTP client sends one request per connection.
- Notifications other than `rpc.call` still get answers (with `"id": null`); this client
  never sends notifications.

## Development

```bash
nix develop                       # python, pytest, mypy; LOGOS_LIDL_CLI and LOGOS_LGX_CLI
pytest tests/unit tests/typing
mypy --strict --python-version 3.10 src tests/goldens tests/typing
nix build .#checks.<system>.<check> -L   # a check below, from the locked inputs
nix run .#regen-goldens           # after a generator change; `-- --check` only compares
nix run .#regen-fixtures          # after a fixture .lidl or lidl change
```

To run any of these against a local checkout of an input, see
[Testing against local checkouts](#testing-against-local-checkouts).

| Check | What it runs |
|---|---|
| `unit`, `unit-py310` | the unit suite and the runtime typing checks on Python 3.13 and 3.10; tests that need the CLIs skip without them |
| `typecheck` | `mypy --strict --python-version 3.10` over `src/`, the goldens and `tests/typing/` (`assert_type` plus negative `type: ignore` lines) |
| `codegen-golden` | regenerates `tests/goldens/` with the pinned `lidl`, and checks that 3.10 and 3.13 produce the same bytes |
| `fixtures-drift` | the vendored ASTs and edge outputs match the pinned `lidl`, and so do the Python identity, validator and serializer mirrors; every vendored copy equals its file in the bridge or test-modules input |
| `lidl-compat` | the pinned `lidl` checks and reads every contract, and adds the identity methods the Python mirror does |
| `integration` | `tests/integration/` against a real bridge (below), except the documents |
| `integration-docs` | `tests/integration/test_docs_conformance.py`: the bridge's documents |

`tests/fixtures/SOURCES.md` records where every vendored file comes from.

### Integration tests

`tests/integration/` drives a real `json_rpc_bridge` under a `logoscore` daemon, with
`test_fullapi_cpp` and `test_fullapi_ext_cpp` loaded first, through
[`logos_bridge.testing.live`](#testing-against-a-real-bridge). `harness.py` holds what is
specific to this suite: the providers, the deny policy, and the tools the contract and
document tests use. It covers:

- both conformance tables through the generic client (cases marked `isolate` on a daemon
  of their own), events, fan-out, and identical refusals for denied, unexposed and
  unknown members; the untyped paths through the daemon's own `modules_state`;
- Host/Origin gating (403 over HTTP, a dropped upgrade over WebSocket), 411 and 415,
  which answers close their connection (those that leave a body unread), the methods
  served as `GET` (these framing checks, `tests/http_framing.py`, also run against
  `FakeBridge`), the body, frame (1009), binary (1003) and batch (-32029) limits, and
  connection slots;
- the lifecycle canary: unload the provider, `SubscriptionTerminated`
  (`provider_unavailable`), `ModuleUnavailable`, load it again, the same view, and a
  fresh subscription;
- the generated clients (`tests/goldens/`) on every non-transport case, refusal parity,
  `check_compat()` (`exact`), and agreement with the dynamic proxy;
- contract identities: the served digests, `lidl json --identity` of the provider's
  `#lidl` output, `sha256(lidl())` = `contract_sha256` = the `#lidl` file = the LGX asset,
  `lidl_reader` = `lidl --version`, and what a deny policy changes (only `exposure`);
- the documents: equal to `json-rpc-bridge-docs` output, valid against the bridge's
  vendored meta-schemas, and describing captured frames, REST bodies and events (mutated
  captures must fail).

Run them in the integration shell. It builds the stack and sets the live stack's
variables (`LOGOS_LOGOSCORE_BIN`, `LOGOS_BRIDGE_INSTALL_DIR`, `LOGOS_LIVE_MODULES_DIRS`)
and those `harness.Stack` adds (`LOGOS_BRIDGE_PROVIDERS_DIR`, `LOGOS_BRIDGE_DOCS_CLI`,
`LOGOS_BRIDGE_METASCHEMAS`, `LOGOS_LIDL_CLI`, `LOGOS_LGX_CLI`, `LOGOS_LIDL_EXPECTED_REV`,
`LOGOS_BRIDGE_FIXES`):

```bash
nix develop .#integration
pytest tests/integration                 # skips everything without the variables
```

- **Required mode.** The nix checks set `LOGOS_BRIDGE_INTEGRATION=required`: a missing
  piece fails, and so does any skip whose reason does not start with `[optional]`.
- **The fixes marker.** `LOGOS_BRIDGE_FIXES=1` says the bridge holds one per-peer slot
  per connection. It enables a strict check: twelve requests over one kept-alive
  connection hold one of the eight slots. A bridge without that fix fails it. This
  package's HTTP client never locks WebSocket clients out either way, because it sends
  `Connection: close`.
- **Documents lag views.** The bridge rebuilds its documents asynchronously, coalescing
  changes for 25 ms, so `rpc.discover` (and the HTTP documents) can briefly lag
  `rpc.schema` and `GET /modules`. A client comparing them should wait for
  `getInfo().docs.generation` to advance, or retry; the harness waits until the documents'
  `x-logos-modules` match the settled views.
- **Teardown.** Each daemon and its module hosts are stopped when their fixture ends,
  also on SIGTERM. If pytest itself is killed, the checks run
  `python -m logos_bridge.testing.live --reap "$LOGOS_LIVE_RUN_DIR"`.
- **macOS.** The checks move `TMPDIR` to `/tmp/lbpy.*`, and the suite applies
  `short_tmpdir_env()` too, because AF_UNIX paths are limited to 104 bytes there. This
  Mac's nix has no sandbox, so run the checks on Linux too.
- **The Linux sandbox.** The checks need no network, and depend on the plugin builds
  (`LOGOS_BRIDGE_PLUGIN_BUILDS`) to bring the bridge's libwebsockets into the sandbox.

### A bridge to develop against

```bash
nix run .#dev-bridge -- --port 8645
```

`dev-bridge` starts a daemon, loads the two providers and the bridge, starts the bridge on
`127.0.0.1:8645` exposing them, waits for their contracts, and prints the URLs and how to
reach the daemon (`TMPDIR=... logoscore --config-dir DIR call ...`: on macOS it runs with
a short `TMPDIR`). Ctrl-C stops the bridge,
the daemon and its module hosts. `--provider NAME` (repeatable) chooses the modules, and
`--config FILE` passes a bridge config of your own (its `http.port` comes from `--port`).

### Inputs

The bridge owns the `logos-lidl` and `logos-package` pins, and this flake follows
them (`logos-lidl.follows = "logos-json-rpc-bridge/logos-lidl"`), so the codegen reads
contracts with the same logos-lidl revision the bridge serves them with. The test
providers come from `logos-test-modules`, the daemon from its `logos-logoscore-cli`,
and the daemon wrapper from `logos-logoscore-py` (source only). `nix flake metadata`
shows the locked revisions.

A relock can change the vendored fixtures' sources: `fixtures-drift` then names each
file to take again (see `tests/fixtures/SOURCES.md`).

### Testing against local checkouts

The checks build from the locked inputs. To test against a local checkout instead, run the
nix command through `scripts/dev-overrides` and name the checkout by its flake URL:

```bash
LOGOS_DEV_BRIDGE=path:../logos-json-rpc-bridge \
  scripts/dev-overrides nix build .#checks.aarch64-darwin.integration -L
scripts/dev-overrides --print      # the flags it adds
```

| Variable | Overrides |
|---|---|
| `LOGOS_DEV_BRIDGE` | `logos-json-rpc-bridge` |
| `LOGOS_DEV_LIDL` | `logos-json-rpc-bridge/logos-lidl`, which `logos-lidl` follows |
| `LOGOS_DEV_PACKAGE` | `logos-json-rpc-bridge/logos-package`, which `logos-package` follows |
| `LOGOS_DEV_TEST_MODULES` | `logos-test-modules` |
| `LOGOS_DEV_LOGOSCORE_CLI` | `logos-test-modules/logos-logoscore-cli`, which `logos-logoscore-cli` follows |
| `LOGOS_DEV_LOGOSCORE_PY` | `logos-logoscore-py` |

`path:DIR` takes the directory as it is on disk, and `git+file://DIR` takes its tracked
files, uncommitted edits included. With no variable set, the command runs unchanged.
With any, the script adds `--no-write-lock-file`: never commit a lock written under an
override. It also fails if nix says an override matched no input (nix only warns about
that).

To try another reader, use `LOGOS_DEV_LIDL`. Overriding `logos-lidl` itself would replace
only this flake's alias while the bridge kept its own reader; the integration stack
refuses that at evaluation. An overridden bridge takes its own inputs from its lock, so
`lidl --version` and the bridge's `lidl_reader` still agree. A `path:` reader has no
revision: both then end in `(unknown)`.

On a machine without the checkout, ship it along and override with a `path:` URL:

```bash
(git archive --prefix=bpy/ HEAD
 git -C "$BRIDGE" archive --prefix=bridge/ HEAD) |
  ssh builder 'd=$(mktemp -d); tar -xi -C "$d"; cd "$d/bpy"
    LOGOS_DEV_BRIDGE=path:$d/bridge \
      sh scripts/dev-overrides nix build .#checks.x86_64-linux.integration -L --no-link
    rc=$?; cd /; rm -rf "$d"; exit $rc'
```

## License

Licensed under either of
[MIT](https://github.com/logos-co/logos-json-rpc-bridge-py/blob/master/LICENSE-MIT) or
[Apache-2.0](https://github.com/logos-co/logos-json-rpc-bridge-py/blob/master/LICENSE-APACHE-v2),
at your option.

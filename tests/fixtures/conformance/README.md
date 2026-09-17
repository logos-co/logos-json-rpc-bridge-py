# LIDL conformance matrix

One question per cell: **does a value of LIDL type `T`, in position `P`, survive
provider `R` → consumer `K` intact?**

The contract being checked is the type contract: one LIDL type maps to exactly
one type per language, numbers are 64-bit only, and bytes ride the canonical
`{"_bytes": "<base64url>"}` tag at every depth.

```
conformance/
  cases.json                the `full_api` case table — language-neutral,
                            shared by every driver
  known.json                its xfail registry: known-broken cells, with evidence
  ext-cases.json            the `full_api_ext` table — records, bytes at depth,
                            typed maps, nested composites
  known-ext.json            its xfail registry
  check_contract_copies.py  asserts the EIGHT hand-maintained copies of the
                            `full_api` contract agree
```

## Two contracts

`full_api` is implemented by **both** providers, and the C++ one is
header-first: its contract is derived from an impl header. The C++ cdylib
backend's `typeSupported()` gate *used to* reject records and `[bstr]` by name,
and its impl-header parser skipped `struct` entirely — so a header-first C++
provider could not even *declare* a record, and adding those types to `full_api`
would have broken `test_fullapi_cpp`'s build rather than adding a test.

They therefore live in `full_api_ext`. logos-cpp-sdk#125 lifted the gate — the
C++ ext provider is header-first too, declares its records as C++ structs, and
`[bstr]` is expressible on both sides — so the split is now a scope decision
(one frozen contract per table) rather than a backend limit.

That table runs **both** providers and carries a provider differential like
`full_api` does. The gap that remains there is the CONSUMER axis: one point
(`py`), no proxy.

The table and the registry live HERE, with the providers they describe. The
drivers live with the client each one uses — the `py` driver is
`logos-logoscore-py/conformance/run_matrix.py` (that repo already depends on
this one; the reverse would be a cycle).

## Running it

```bash
# from logos-logoscore-py, or via its `conformance-matrix` flake check
python3 conformance/run_matrix.py \
  --cases    <logos-test-modules>/conformance/cases.json \
  --known    <logos-test-modules>/conformance/known.json \
  --contract <logos-test-modules>/test-fullapi-proxy-module-rust/full_api.lidl \
  --cpp-modules  <test_fullapi_cpp.install>/modules \
  --rust-modules <test_fullapi_rust.install>/modules \
  --proxy-consumer qtproxy-sync=test_fullapi_qtproxy=<install>/modules=sync \
  --proxy-consumer qtproxy-async=test_fullapi_qtproxy=<install>/modules=async \
  --jsonl  matrix.jsonl \
  --report matrix.html \
  --md     known-broken.md
```

The two `--proxy-consumer` points are what make the consumer axis exist; without
them the run measures `py` alone and no consumer differential is computed.

The ext table is run the same way, with its own contract, its own registry and
its own proxy — `test_fullapi_ext_qtproxy`, which binds `full_api_ext`:

```bash
python3 conformance/run_matrix.py \
  --cases    <logos-test-modules>/conformance/ext-cases.json \
  --known    <logos-test-modules>/conformance/known-ext.json \
  --contract <logos-test-modules>/test-fullapi-ext-module-rust/rust-lib/test_fullapi_ext_rust.lidl \
  --modules test_fullapi_ext_cpp=<install>/modules \
  --modules test_fullapi_ext_rust=<install>/modules \
  --proxy-consumer 'extqtproxy-sync=test_fullapi_ext_qtproxy=<install>/modules=sync=echoStringMap:[{"k":"v"}]' \
  --proxy-consumer 'extqtproxy-async=test_fullapi_ext_qtproxy=<install>/modules=async=echoStringMap:[{"k":"v"}]'
```

The fifth field of `--proxy-consumer` is the CALL-MODE PROBE — the method whose
`lastCallStatus()` proves the selected wrapper table actually ran. It defaults
to `echoInt:[1]`, which only `full_api` has; `full_api_ext` has no method taking
a bare scalar, and its one zero-parameter method silently ignores an extra
argument (`known-ext.json` **B-arity-overflow**), so probing it with a spare `1`
would lean on a registered defect and stop working the day that defect is fixed.

Exit status is non-zero if any cell is `fail`, `xpass`, `uncovered`,
`dead-skip` (a `skip[]` pattern that matches no case in the table),
`skip-passes` (a cell declared unsupported on a surface that in fact answers
it), or `setup-failed` (a consumer that could not be pointed at a provider).
The last three exist because each one is a way for the run to look green while
measuring less than it claims.

## Reading the report

A red cell is a **coordinate**, never an aggregate token:

```
fail      [uint]/method_arg,method_return/test_fullapi_rust/py
          case=[uint]/boundary/max
          expected=[18446744073709551615]
          actual  =[1.8446744073709552e+19]
```

`type / position / provider / consumer`. That shape matters: the previous
arrangement asserted `ALL_OK` over the *combined* output of both providers, so
one provider alone could satisfy the assertion while the other was broken.

## What keeps it honest

**Differential comparison.** Every case runs against *both* providers and their
answers are compared to each other, independently of `expect`. This needs nobody
to know the right answer in advance, and it is how the `void` divergence was
found. A case whose providers legitimately differ declares that with
`expect_by_provider`; anything else disagreeing is a failure.

The same comparison runs on the **consumer** axis, over every pair of consumer
surfaces per provider. That is not a symmetry for its own sake — it is where the
two most recent findings came from (`M3`, `Q1` in `known.json`), because a value
that survives one consumer's decode can be destroyed by another's.

**`xfail` is a registry, not a deletion.** A known-broken cell lives in
`known.json` with its measurement and evidence. If it starts passing, the run
reports `xpass` and **fails** — so the registry gets updated when a fix lands
instead of the fix going unnoticed.

**Coverage is computed from the contract.** `--contract` parses the `.lidl` and
fails the run if any declared `(type, position)` has no case. This is the
structural guard against the state this replaced: a 31-method contract with six
assertions.

**Comparison is type-strict.** `same()` refuses `1 == True` and `1 == 1.0`. A
matrix that compares with `==` cannot see an integer degrading to a float, which
is most of what it exists to catch.

**Values, not shapes.** An assertion on `len(result)` is not coverage. The
drivers this replaces reported `intList=3 uintList=2`, which passes whatever the
elements became.

## Adding a case

Add a row to `cases.json` naming the cell it covers:

```jsonc
{ "id": "uint/boundary/max", "type": "uint", "position": "method_arg,method_return",
  "method": "echoUint", "args": [18446744073709551615], "expect": 18446744073709551615,
  "tags": ["boundary"], "why": "above int64max there is no signed representation" }
```

- **Bytes** are written `{"_bytes": "<base64url>"}` and materialized by the
  driver into its native byte type. `"_bytes": "__ALL_BYTES__"` expands to all
  256 byte values.
- **`"raw": true`** opts out of that materialization. It is what makes the
  adversarial cases expressible: "a *user* map that happens to have a single
  `_bytes` key must come back as a map". Without it the driver would convert
  both the argument and the expectation to bytes and the cell would compare
  bytes to bytes and pass no matter what the system did.
- **`expect_by_provider`** pins a divergence rather than papering over it.
- **`"module": "<name>"`** re-targets one case at a different module. It exists
  for exactly one thing — a class-A transport failure, which cannot be stated by
  naming a method, only by naming a module that is not loaded. The cell's
  provider/consumer coordinate then names the environment the call was made in,
  not the callee.
- **`"isolate": true`** runs the case in a daemon of its own. Use it when a case
  can leave the daemon WORSE than it found it: `failure/A/module-not-loaded`
  spends 20s in an acquire that retries to its deadline, and the calls after it
  in the same daemon slow down measurably. Without a daemon of its own, the
  cells that follow are a measurement of the case before them, and nothing in
  the table says so — the dependency is on list ORDER.
- Adding a *type* costs one LIDL line, one impl method per provider, and N table
  rows — **zero per-consumer cost**. That property is the point: the failure
  mode being designed out is structural, not laziness.

### Adding a case that must FAIL

`"expect": {"__error__": "<code>"}`. There is no `expect_error` key — the schema
comment used to document one, nothing ever implemented it, and a case written
that way was filed `skip` and asserted nothing. `validate_table` now rejects any
key the driver does not read.

`<code>` is a failure CLASS, not the envelope verdict. A failed `logoscore call`
prints two codes:

```json
{"status":"error","code":"METHOD_FAILED",
 "error":{"code":"invalid_args","message":"expected 1 arguments, got 0","origin":"m"}}
```

`code` is METHOD_FAILED for every way a call can fail; `error.code` says which
way. `run_matrix.py`'s `error_code_of` prefers the second and falls back to the
first, so a case may assert either spelling and `{"__error__": "METHOD_FAILED"}`
now means "some failure" rather than a class. Prefer the class.

## The failure classes

The type contract answers "did this value survive". These cases answer the other
question: **can a caller tell "the provider answered, and the answer is nothing"
from "the call did not succeed"?** Five classes, each with a case family:

| class | what happened | how it reaches the caller | cases |
|-------|---------------|---------------------------|-------|
| **A** | the call never reached a provider | `LP_ERR_UNAVAILABLE` + an error object: `object_unavailable`, `timeout`, `transport_error`, `call_failed`, `unauthorized` | `failure/A/*` |
| **B** | the provider ran and refused the argument COUNT | `invalid_args`, as a 3-key `{code,message,origin}` object arriving as the RESULT and folded into the error channel by the consumer | `failure/B/*` |
| **C** | the method NAME is unknown | **a bare null with `LP_OK`** | `failure/C/*` |
| **D** | the answer is legitimately empty | **a bare null with `LP_OK`** | `failure/D/*`, `Optional/scalar/empty-is-null` |
| **E** | the provider ran and refused the argument VALUES | `dispatch_failed`, same 3-key object | `hostile/*`, `adversarial/*` |

C and D are the same bytes. That is not a defect of one component — it is stated
in `logos-protocol` (`cpp/logos_protocol.h`, and pinned by
`tests/protocol/test_call_error_after_acquire.cpp:391`), the cdylib dispatch ends
in `return nullptr;  // unknown method`, and no error channel and no rejection
detector can see the difference. Anything that separates them does so OUT OF
BAND — on a null return the daemon asks the module for its method list and
answers `METHOD_NOT_FOUND` when the name is absent.

That rescue is `logos-logoscore-cli` #99 and **the daemon this table is pinned
to does not have it**: at the pins, classes A, C and D are one indistinguishable
`METHOD_FAILED`, and the cells that say so are registered together under
`pre-99-null-is-not-an-error` in both registries. They retire on one lock bump,
in one edit, along with `known-ext.json`'s `OPT2`.

Three things the families keep executable. Each one had already changed its
answer by the time it was measured at this fixture rev, which is the argument
for cases over prose:

1. **A caller that infers failure from a null RESULT breaks class D.** That was
   real on the `py` path, and `failure/D/null-is-a-value` and `OPT2` are the two
   spellings of it. All six of its cells answer the same `METHOD_FAILED` — the
   daemon's, not the Qt hop's, which took an A/B to establish (see the case, and
   the note in `pre-99-null-is-not-an-error`).
2. **The rescue needs the name to be ABSENT.**
   `failure/C/identity-arity-is-a-bare-null` is the case where it is PRESENT.
   When it was written the provider answered a bare null there and no
   introspection could help — the class-C residual. Not any more: the identity
   methods moved into the cdylib dispatch, `version("junk")` returns `"1.0.0"`,
   and the residual is retired by measurement. What replaced it is class B in a
   dispatch generated separately from the contract methods
   (`B-arity-overflow-identity`) — arguably worse, because a wrong arity now
   gets a correct-looking answer instead of nothing.
3. **An EXTRA argument is dropped, everywhere.** Three cases here and two in
   `full_api_ext`, on four modules built from two contracts by two language
   backends, no coordinate dissenting (`B-arity-overflow`). The UNDERFLOW half —
   one argument too few — passes at this rev and did not when these cases were
   written, so `failure/B/arity/too-few` is carried unregistered, as the
   regression guard for the guard.

<!-- generated by run_matrix.py --md; do not edit by hand -->
## Current known-broken cells

Measured on consumer(s) `py`, `qtproxy-async`, `qtproxy-sync` against provider(s) `test_fullapi_cpp`, `test_fullapi_rust`. See `known.json` for the full measurements and evidence.

| id | cells in this run | cases | what |
|----|----|----|----|
| M4-residual | 6 | `adversarial/any/pending-call-canonical` | a CANONICAL-shape forgery of the deferred-call sentinel still hijacks a call |
| M3 | 4 | `adversarial/{tstr:any}/_bytes-key` | a one-key `_bytes` map reaching a Qt-typed map slot is reinterpreted as bytes and arrives EMPTY |

### Closed

Kept because it explains why several green cases exist at all: they are the regression guards a fix left behind.

| id | fixed by | what |
|----|----|----|
| M1 / M1b / M5 | logos-protocol 362b03f — one canonical LIDL <-> JSON codec (#29), pinned here via logos-cpp-sdk 3d322bd / logos-module-builder / logos-logoscore-cli | a uint64 above int64max degrading to a double once nested (M1/M1b), and a bstr nested in a container being UTF-8 mangled (M5). Both were the same root cause: the Qt and plain-wire paths each had their own conversion, and neither preserved a value whose type the container did not declare. |
| M6 | logos-protocol — setEventListenerStdBridge now parses with nlohmann and converts via logos::nlohmannArgsToQVariantList, the same helper callMethodStdBridge uses | a uint64 above int64max degrading to a double on the EVENT path (uintEvent(2^64-1) -> 1.8446744073709552e+19) while the method path was exact. |
| bytes-on-the-event-path | the same change | canonical tagged bytes {"_bytes": ...} were not decoded by the event bridge — they arrived as a QVariantMap where the method path yields a QByteArray. |
| M2 | logos-qt-sdk cdylib glue (kVoidMethods) + logos-rust-sdk void arms | a `void` return answered `true` from the C++ provider and METHOD_FAILED from the Rust one. |
| M4 | logos-protocol — logos::isPendingCallSentinel, replacing a bare contains() at all four detection sites | a user map that merely CARRIED the sentinel key was taken for a deferred call. |
| pre-99-null-is-not-an-error | logos-logoscore-py relocking logos-logoscore-cli onto master (0f0be25), which carries ed19258 (#99) and b3f1a403 (#101). | Classes A, C and D were one indistinguishable METHOD_FAILED because CoreServiceImpl::callModuleMethod took the invokeRemoteMethod overload with no CallError* and decided the verdict by testing ret.is_null() on the RESULT. |
| B-arity-overflow | logos-plugin-qt#29 (f668ef2), carried by logos-module-builder 6044ffc. The provider half was logos-cpp-sdk#150 and logos-rust-sdk#50; this is the consumer half. | An EXTRA argument was dropped and the call succeeded. The last six cells were failure/B/arity/too-many-zero-parameter on `doVoid`, where the provider refused correctly and the Qt host glue discarded the refusal. |
| B-arity-overflow-lidl-builtin | logos-plugin-qt#42 (e46678a), carried by logos-module-builder#252 (8007924). The generated C++ and Rust dispatches already refused the call; the glue answered first. | lidl("junk") answered null with status ok. The Qt host glue answers the lidl() built-in before the module's dispatch runs, and its own argument check returned an empty QVariant. |

### Not measurable by this matrix

| id | why |
|----|----|
| M3-history | M3 is no longer unmeasurable — it moved to `xfail` with a coordinate. This is the record of why it sat here, because the reason is a reusable lesson about what a matrix can and cannot see. |

### Retired

Hand-written, and deliberately OUTSIDE the generated block above: `--md` renders
`xfail`, `fixed` and `unmeasurable`, and has nothing to say about an entry that
stopped describing anything. `known.json`'s `retired[]` carries the measurement
that retired each one — not deleted, because each records a prediction that
turned out wrong.

| id | retired because |
|----|----|
| Q1 (12 cells) | all 12 cells PASS, so the entry reported `xpass` and kept the run red. It described a Qt-typed PROVIDER dispatch, where `[uint]` and `[any]` are both `QVariantList`; that dispatch left the fixture when `test_fullapi_qtproxy` became `interface: "universal"` and inbound arguments started going through the C++ cdylib decode (`logos::fromJson<T>`), which HAS the element type. |
| Q1b (4 cells) | the divergence it registered no longer exists: logos-cpp-sdk 853a261 made the cdylib container decode shape-check, so both providers now answer `dispatch_failed` and the `expect_by_provider` split collapsed to one expectation. |
| qtproxy-wrapper-axis | `useWrapper`/`currentWrapper` are gone — the generated Qt wrapper IS the veneer now, so the axis ran the same code twice. |

The block above the Retired section is GENERATED and is now pasted verbatim.
`run_matrix.py --md` writes it, the flake check drops it at
`$out/known-broken.md`. It was hand-maintained until it drifted to eleven rows
against a registry of four — listing M1/M1b, M2, M4, M5/E1, E2, E3 and OPT1 as
current long after each moved to `fixed[]`, while omitting the one live entry
with the most cells. Regenerate rather than edit:

```bash
nix build .#checks.<system>.conformance-matrix   # in logos-logoscore-py
cp result/known-broken.md conformance/README.md  # the section body
```

### Cases written before the implementation

The `Optional/` family was written contract-first: the expectations were
derived from the LIDL contract before any generator read `?T`, so at the time
they were red on purpose — the point of writing them first is that the
implementation has a target to hit rather than a behaviour to bless. That
landed. Both ext providers now implement optionality, all 34 Optional cells
are measured against them, and `known-ext.json` keeps the record under
`fixed[].OPT1`. Four of those cells are still registered, for two unrelated
reasons: `OPT2` is the empty `?tstr` RETURN, waiting on the same daemon lock as
the class-A and class-C cells beside it, and
`ext-optional-return-changed-on-one-provider` is newer and is not about
optionality at all — `echoOptional`'s return moved from `?tstr` to `result` in
the Rust LIDL and not in the header-first C++ provider, so the two providers of
one contract answer different shapes.

## Other drivers

`logos-logoscore-py/conformance/run_matrix.py` runs the whole consumer axis in
one process — it has to, because the consumer differential only exists if the
surfaces are measured together. A consumer point is either the direct client
(`py`) or a forwarding module:

```
--proxy-consumer qtproxy-sync=test_fullapi_qtproxy=$DIR=sync
--proxy-consumer qtproxy-async=test_fullapi_qtproxy=$DIR=async
```

The proxy is loaded, pointed at each provider with `useProvider`, and — for a Qt
proxy — put in a call mode with `useCallMode`. Every setting is **read back**,
and a call mode is additionally proved by `lastCallStatus()` reporting
`ok-sync`/`ok-async` on a known-good call: a `useProvider` that silently no-ops
would make both providers measure identically, and a `useCallMode` that no-ops
would make the async half of the matrix a duplicate of the sync half. Neither
failure announces itself.

Still unwired: the QML bridge (the `skip[]` entries describe that surface) and
the Rust/cdylib proxy.

## Consumer surfaces

A proxy only adds a consumer *coordinate* if it reaches a different generated
client. Two of the three do not, which was measured before this table existed —
replaying the whole case table through the universal and cdylib proxies moved 2
cells out of 86.

| module | metadata | generated client | reaches |
|--------|----------|------------------|---------|
| `test_fullapi_proxy` | `interface: universal` | apiStyle=**lp** (Qt-free, `logos::LpClient`) | the C ABI / plain wire |
| `test_fullapi_proxy_rust` | `interface: cdylib` | the **Rust** client | the Rust decode |
| `test_fullapi_qtproxy` | `interface: universal` + `codegen.consumer_api_style: "qt"` | apiStyle=**qt** (`LogosAPIClient`, `QByteArray` / `qulonglong` / `QVariantList` / `LogosResult`) | `logos_json_convert.cpp` and the generated sync/async return tables |
| `test_fullapi_ext_qtproxy` | the same two keys, against `full_api_ext` | apiStyle=**qt**, on the WIDENED spellings | the generated record codecs and every typed-container element loop |

The last row is a separate module and not a second binding of the first,
because a Qt consumer wrapper is generated per CONTRACT. It is also where the
lossless Qt mapping actually lives: `full_api` has no record, no typed container
and no optional in it, so `FullApiExt::Blob`, `QList<Blob>`,
`QMap<QString, Blob>`, `QList<QByteArray>`, `QList<QList<qlonglong>>`,
`QMap<QString, QList<QByteArray>>` and `std::optional<QString>` are reachable
from `full_api_ext` and from nowhere else. Each of them is emitted as an element
LOOP rather than handed to the codec whole — a typed `QList` matches none of
`qvariantToNlohmann`'s closed `userType()` set — so it is a body of generated
code the `qtproxy-sync`/`qtproxy-async` points cannot execute.

Its metadata used to read `type: core` with **no `interface` key** — one decision
selecting the Qt surface for the provider AND the consumer at once, and the last
caller of `logos-cpp-generator --provider-header`. That mode was removed; the two
axes are declared separately now, so the PROVIDER half is a header-first cdylib
like every other fixture and only the CONSUMER half is Qt-typed.

The Qt one is the only surface that reaches the `_bytes` reinterpretation in
`nlohmannToQVariant` (registry entry **M3**), and the only one that drives the
generated ASYNC return table at all. `useCallMode sync|async` routes every
forwarded method through one table or the other, so the whole case table replays
twice; `syncProbe()` and `probeAsync()`/`getAsyncProbe()` remain as a fixed
type-tagged rendering of 18 calls, which the forwarding path cannot show because
the value is re-encoded on the way back.

**Measured, so it is not re-argued:** over the whole table the two tables agree
on every cell — 0 deltas across 68 method cases x 2 providers. The difference
between `_result.toT()` and `qvariant_cast<T>(v)` is real in the generated
source and is now driven; it does not currently change an answer. What the Qt
surface changes is this, and none of it is mode-dependent:

| what | cells (per Qt consumer: cases x 2 providers) | where it happens |
|------|--------------------------------------------|------------------|
| **M3** — a one-key `_bytes` map into a typed map slot arrives `{}` | 1 x 2 = 2 | `nlohmannToQVariant`; invisible on an `any` slot, where the transformation is its own inverse |
| M4-residual | 1 x 2 = 2 | pre-existing, identical on every consumer |

It was 22, then 12, and is now 4. Ten of the original cells were **Q1**'s
scalar/container shapes; they closed when both Qt provider sites stopped coercing
arguments and started decoding them through the canonical codec. Two more did not
close, they INVERTED — **Q1b** — and each count was the measured one, not the
predicted one. The last eight went when both entries moved to `retired[]`: Q1's
remaining 12 registry cells (6 per Qt consumer) all pass, its Qt-typed provider
dispatch having left the fixture with the universal migration, and Q1b's
divergence collapsed when the C++ cdylib container decode started shape-checking.
The rule the Qt provider sites now apply is the codec's own, which matters
because a naive "reject anything inexact" gets it wrong: a whole-valued `3.0` is
a legal integer (JSON does not distinguish it from `3`, and this CLI produces it
for the spelling `3.0`) while `3.7` is not.

All of them were attributed by replaying the same table through the **lp** proxy,
which is a hop of the same shape on a non-Qt client: it answered
`dispatch_failed` on all nine hostile cases and preserves the `_bytes` map. So
the extra hop was never the cause — the Qt api style was.

Note what the Qt *consumer* wrapper cannot express, so a cell that looks green
there is read correctly: `[any]`, `[int]`, `[uint]`, `[float64]` and `[bool]` are
all `QVariantList`, and a `void` return becomes `QVariant(true)`.

That collapse used to reach the PROVIDER side too, and it was the whole of what
Q1 had left: a provider can only check an argument against the type it can *see*,
and the Qt spelling of a typed numeric array has already thrown the element type
away. It no longer applies here — `test_fullapi_qtproxy`'s provider half is a
std-typed impl header decoded by `logos::fromJson<T>`, which has the element
type, which is why Q1's cells now pass. `check_contract_copies.py` records the
same move: it used to compare the `qtproxy-h` copy by COMPATIBILITY under a
`cpp-qt` parse kind that encoded the collapse; that kind is gone, and the copy's
33 methods are now compared for EQUALITY with its 15 events compared at all.

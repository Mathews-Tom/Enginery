# Glossary

Domain terms used in Enginery's module names. Persisted identifiers keep their historical spellings: ledger aggregate types, node ids such as `stage1.validate`, and class names such as `Stage1RunService` still say `stage1`.

**Stage 1**: the issue-to-merge-ready-pull-request workflow. A historical name kept in persisted identifiers and class names; module paths no longer use it.

**Run**: one durable execution of Stage 1 for one GitHub issue, identified by a run id and bound to an immutable request with a digest.

**Run request**: the immutable, digest-bound input of a run (work item, configuration, runner, execution selection). Compiled by the request compiler, decoded with `stage1_request_from_state`.

**Run lifecycle** (`enginery.workflows.run_lifecycle`): the module that holds run requests and their codecs and the service that derives each run's next action from ledger state and advances it: qualification, implementation, validation, review, publication, CI repair and merge readiness, including holds, human waits and cancellation precedence.

**Node executor** (`enginery.workflows.node_executors`): the coordinator-owned executors for the deterministic run nodes: qualification, sanitized validation and review.

**Request compiler** (`enginery.cli.request_compiler`): builds a run request from a GitHub issue and the effective configuration; backs `enginery engine build-request` and project `run`.

**Run composition** (`enginery.cli.run_composition`): wires the run lifecycle service to its runtime, harness, GitHub ports, artifact store and publisher, separating read-only from advancing services.

**Runner wiring** (`enginery.cli.runner_wiring`): the isolated runner's engine-facing pieces for a run: runner context, admission, in-image probe and runner-confined validation.

**Execution diagnostics** (`enginery.cli.execution_diagnostics`): the report that keeps a run's requested, selected, submitted and effective execution controls apart.

**Engine runs** (`enginery.cli.engine_runs`): the `enginery engine` commands that start, inspect and advance durable runs.

**Stack status** (`enginery.cli.stack_status`): `enginery engine status`, a read-only view of one plan's release stack (the Stage 2 workflow).

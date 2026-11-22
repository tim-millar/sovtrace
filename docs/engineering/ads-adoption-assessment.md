# SOVTrace assessment of Agentic Developer Setup

## Executive summary

**DEFER public adoption.** Agentic Developer Setup (ADS) assessed SOVTrace at the repository root. Its Python adapter added interpreter, packaging and command evidence, recovered Pyright, and preserved four documented `uv run --locked` invocations. A baseline-only, metadata-only proposal passed plan, preview, check and dry-run. The adapter transaction stopped at the human selection-review gate. ADS redistribution rights remain unresolved, and the adapter's CI and credential findings need independent interpretation before any binding is approved. [SOVTrace issue #9](https://github.com/tim-millar/sovtrace/issues/9)

## Scope and limitations

| Stage | Result |
| --- | --- |
| Static assessment | Generic assessment, adapter detection and explicit Python selection completed successfully. Findings were compared with the pinned target. |
| Non-mutating planning | The baseline ownership proposal passed all four stages. No reviewed adapter or specialised-file plan was generated. |
| Installation | No ADS installation, mutating apply, or canonical adoption metadata creation occurred. |
| Operational validation | ADS-assisted setup, command execution, agent launching, reviews, updates and rollback were not established. Separate native SOVTrace checks are listed below. |
| Productivity | No measurement was made. |

The assessment covers one clean SOVTrace revision and one ADS revision. It excludes live provider use, distribution of ADS source and tests of installed ADS workflows.

## Repository and environment baselines

| Checkout | Pinned revision and state |
| --- | --- |
| ADS, `agentic-developer-setup` | `bf51905d77f892cfcc9c6f8784214ce9a1e9548b`, clean detached checkout of merged `main`. ADS framework version 0.1.0, catalogue schema 2. |
| SOVTrace, `sovtrace` | `28a30f308875569d7e4d328dda68801f888717dc`, clean detached checkout of merged `main`, also the commit named by `v0.1.0`. |
| SOVTrace report worktree | Clean at the SOVTrace baseline before authoring on `issue-9-ads-assessment`. |

On 22 November 2026 UTC, local Git roots, origins, HEADs and porcelain status were checked independently against the environment record. GitHub confirmed both main heads. ADS #12 was closed as completed and merged via ADS #109. SOVTrace LQ-06 was closed as completed and its merge commit `3d824a8f4e1f0f2b5c7b490321742cb28e752980` is an ancestor. [ADS #12](https://github.com/tim-millar/agentic-developer-setup/issues/12), [ADS #109](https://github.com/tim-millar/agentic-developer-setup/pull/109), [SOVTrace LQ-06](https://github.com/tim-millar/sovtrace/issues/4)

The assessment used arm64 macOS 15.6, Git 2.50.1, Ruby 3.3.12, Bundler 2.5.22 and the existing frozen ADS bundle (`tomlrb` 3.0.0). System Ruby 2.6.10 was bypassed by selecting the pinned interpreter. The Python provider was `ecosystem/python` 1.0.0 and the registered Node provider was 1.0.0. Native verification used Python 3.12.7 and 3.12.10, uv 0.12.7, Ruff 0.17.1, Pyright 1.1.414 and pytest 9.1.1.

## Reproducible methodology

Inspect the target's tracked files and command definitions before interpreting ADS output. Use separate, non-overlapping checkouts. From a parent containing `ads-source`, `sovtrace-target` and an existing external `artifacts` directory, select Ruby 3.3.12 with the pinned bundle already provisioned. Record `git --no-optional-locks status --porcelain=v1 --untracked-files=all` and `git rev-parse HEAD` for both checkouts before and after the runs.

```sh
ads="$(cd ads-source && pwd -P)"
target="$(cd sovtrace-target && pwd -P)"
out="$(cd artifacts && pwd -P)"
cd "$ads"
umask 077
export BUNDLE_FROZEN=true BUNDLE_PATH="$ads/vendor/bundle" BUNDLE_IGNORE_CONFIG=1
export GIT_OPTIONAL_LOCKS=0
bundle _2.5.22_ check
bundle _2.5.22_ exec ruby scripts/assess_repository.rb --help
bundle _2.5.22_ exec ruby scripts/assess_adapters.rb --help
bundle _2.5.22_ exec ruby scripts/assess_repository.rb "$target" \
  --output "$out/generic.yml" --report "$out/generic.md"
bundle _2.5.22_ exec ruby scripts/assess_adapters.rb "$target" \
  --output "$out/adapter-detection.yml"
bundle _2.5.22_ exec ruby scripts/assess_adapters.rb "$target" \
  --selection "$out/python-selection.yml" --output "$out/python-assessment.yml"
```

Before the final command, prepare an external `adapter-selection-v1` document per `schemas/adapter-selection-v1.schema.json`. Use `schema_version` 1, repository scope with path `.`, a single `ecosystem/python` entry with source `explicit`, and empty `inputs` and `resolutions`. This selects assessment scope. Transaction review is a separate step. The direct CLI is the documented implementation behind `make assess-adapters REPO=…` and supports selection and external output. All three assessment invocations returned 0. Raw outputs, decisions and plans were kept externally with private file permissions.

## Independent SOVTrace evidence

| Contract | Direct evidence |
| --- | --- |
| Interpreter and dependencies | `.python-version` selects 3.12, `pyproject.toml` declares compatibility `>=3.12`, and both workflows select 3.12. `uv.lock` is committed. `CONTRIBUTING.md` specifies `uv sync --locked` and locked invocations. The compatibility range does not establish tested support for newer interpreters. |
| Package and CLI | `pyproject.toml` declares Hatchling and the installed `sovtrace` entry point `sovtrace.cli:main`. `src/sovtrace/cli.py` implements the CLI in a `src` layout. Hatchling is the build backend and uv is the manager. Static metadata does not establish a successful build. |
| Native validation | `pyproject.toml` configures pytest under `tests`, Ruff for `py312` and Pyright for Python 3.12 across `src`, `tests` and `scripts`. `README.md` and `CONTRIBUTING.md` document direct uv invocations. No tracked Makefile, Taskfile, hooks or aggregate check wrapper was found. |
| Offline and live execution | `.github/workflows/offline.yml` installs the lock before offline checks, removes model configuration variables and sets `UV_OFFLINE=1` and `PYTHONPATH` for `.ci/offline_guard/sitecustomize.py`. The Python audit hook blocks selected socket operations. Smoke commands, `scripts/demo.py`, `scripts/case_study_demo.py` and `scripts/verify_synthetic.py --offline` use installed or synthetic project code. The evaluator selects the offline branch explicitly. Live interpretation and `scripts/verify_schema_live.py` have separate credential and disclosure requirements. |
| Security and application boundaries | `.github/workflows/security.yml` runs read-only CI with checksum-pinned Gitleaks 8.30.1 and an isolated, hash-pinned pip-audit 2.10.1 toolchain. `.ci/scan_secrets.py` and `.ci/audit_dependencies.py` enforce scan completion and fail closed. The audit needs advisory-service access outside offline verification. Repository docs and `src/sovtrace/{ingestion,qa}/` describe and implement bounded input, restricted SQL, exact monetary handling and source evidence. |
| Docs and authority | `CONTRIBUTING.md`, `README.md`, `SECURITY.md`, `docs/ARCHITECTURE.md` and `docs/ENGINEERING_CASE_STUDY.md` supply development, test, domain and security evidence. `CONTRIBUTING.md` and issue #9 supply contribution and task-specific review rules. No tracked standing `AGENTS.md`, `REVIEW.md`, review skill, PR template or Codex launcher was found. |
| Local artifacts | `.gitignore` covers local environments, tool caches, generated databases and restricted input directories. No environment, caches or generated database were present in the assessment target. |

## Generic ADS assessment

| ADS output | Classification and independent comparison |
| --- | --- |
| `repository.git`, `scope`, `ecosystem` | **Correct.** Reports the pinned clean commit, unknown branch for detached HEAD, a single project at `.` and high-confidence Python evidence from `pyproject.toml` and `uv.lock`. |
| `tooling.package_managers`, `test_frameworks`, `linters`, `formatters` | **Correct.** Finds uv, pytest and Ruff lint/format signals. `validation.capabilities.tests` also records test-directory and CI evidence. These are static observations. |
| `tooling.type_checkers`, `validation.capabilities.static_type_checking` | **Material omission.** Empty and `not_detected` despite `[tool.pyright]`, `pyright[nodejs]` and explicit local and CI invocations. The generic detector's type-checker recognisers cover a narrower tool set. |
| `tooling.command_surface`, E012 | **Incorrect.** Lists `make estimates` as a documented command. `docs/ARCHITECTURE.md` uses those words in a sentence about accounting. No Makefile defines the target. |
| `validation.ci_alignment`, `build_compile` | **Partial evidence.** Both workflows and overlapping pytest/Ruff/uv tokens are real. The high-confidence `ready` alignment is coarse and misses Pyright, exact invocation arguments and offline/security environment differences. Hatchling configuration supports build intent. Build execution was outside the assessment. |
| `documentation`, `framework_adoption`, `component_recommendations` | **Mixed.** Correctly finds the development guide and CI as native and adoption metadata and agent instructions as absent. Marks architecture `framework_like` by recognised path and recommends specialisation, despite extensive native content. Recommends adopting the test and domain docs even though these functions are already covered across SOVTrace's docs and CI. Path recognition supplies no ADS provenance. |
| `readiness`, `tier_recommendation` | **Advisory and partly ambiguous.** Tier 1 is a catalogue judgement. Partial command and review-handoff findings indicate missing dedicated surfaces. Existing documented uv commands, contribution guidance and task review are usable. Offline application semantics, installed tooling and exact compatibility policy are outside the generic result. |

Relevant generic rules are in ADS `docs/repository-assessment.md` and `lib/agentic_developer_setup/assessment/detector.rb`. The independent dispositions below use SOVTrace's actual repository contracts.

## Python adapter assessment

The published provider is registered as supported in `framework.yml` and `adapters/ecosystems/python/adapter.yml`. Node and Python are the only registered supported providers at this revision. Ruby, Elixir, Docker and non-containerised entries are planned. Detection reported Python at `.` with high confidence and no ambiguities, Node as not detected, and no selection or composition. Explicit selection reported compatibility and composition `ready` with no conflicts. `selected` bound provider version and manifest/provider digests. It needed no manager override despite the optional `ecosystem/python.package_manager` input.

| Capability | Expected and observed | Assessment and adoption consequence |
| --- | --- | --- |
| Identity and scope | `pyproject.toml`, `.python-version` and `uv.lock` support a single Python project. `candidates` and `selected` agree at `.`. | **Correct.** No workspace boundary or second ecosystem to resolve. |
| Interpreter | `python-compatibility` and interpreter evidence distinguish `>=3.12` from local and CI `3.12` selectors. `python-version-unresolved` leaves selector/range compatibility open. | **Correct support boundary.** Expected conservative result for selectors. No incompatible exact pins were reported. Keep 3.12 as the operational policy. |
| Manager and environment | `python-manager-uv` identifies uv from `uv.lock`. The four recommendations retain `uv run --locked`. No conflicting manager or standalone uv version pin was asserted. | **Correct.** Preserve the lock and use CI's uv 0.12.7. The provider does not verify lock freshness, installation or interpreter provisioning. |
| Layout, package and application | `python-distribution`, `python-src-layout` and `python-entrypoints` identify the static build backend, source layout and possible installed CLI from `pyproject.toml`. | **Correct and bounded.** The declared entry point matches `src/sovtrace/cli.py`. Distribution build, exact package discovery and application behaviour remain unverified. |
| Test, lint, format and typing | `python-tool-pytest`, `python-tool-ruff` and `python-tool-pyright` recognise declarations and local commands. The two Ruff invocations distinguish lint from formatting. Pyright's exact include paths come from `pyproject.toml`. | **Correct.** Repairs the generic Pyright omission. Configuration and command eligibility do not establish coverage. |
| Native wrappers and unsupported checks | `composition.commands` contains four documented invocations from `README.md` and `CONTRIBUTING.md`. No Make, extra wrappers or invented commands. `python-unsupported-capabilities` reports no eligible build or verify recommendation. | **Correct.** Preserve CLI smoke, demos, synthetic evaluation and security checks separately. The supported command grammar does not model these SOVTrace scripts as a verification suite. |
| CI comparison | Two `python-ci-dynamic-*` findings mark incomplete interpretation. Four `python-ci-missing-*` findings report no match in the inspected subset, although `.github/workflows/offline.yml` contains all four exact invocations in one guarded multiline step. | **Intentional support limit with misleading missing diagnostics.** The environment expression and multiline shell are unsupported. The generic assessor's `ready` and Python's incomplete comparison answer different questions. Manual comparison confirms argv overlap and different environment controls. |
| Setup and credentials | `python-risk-setup` flags setup/build paths. `python-risk-credentials` points to the optional live example in `README.md`, while the offline CI step clears credentials. `composition.contributions.validation_profiles` is empty. | **Real optional risk, ambiguous breadth.** The provider suppresses profiles repository-wide for this signal. Keep the individual commands advisory and preserve credential isolation. |
| Paths | `composition.contributions.paths` is empty. The checkout has no observable conventional caches or generated directories. | **Correct.** The provider classifies observable paths rather than interpreting all ignore patterns. Custom database paths and private input boundaries require repository guidance. |
| Static safety | Provider source parses files, and `test/adapters/python_test.rb` with `test/adapters/python_support.rb` exercises bounded reads, no configured clean filters and process/network guards. Assessment used the pinned providers and external outputs. | **Supported within observed scope.** No target execution, imports, builds, installation, provider calls or target mutations were observed. This relies on code, tests and worktree checks. System-level tracing was outside scope. |

The generic assessor supplies repository, basic tooling and component evidence independently. Python adds interpreter distinctions, packaging and entrypoint signals, exact scoped argv, applicability preconditions and stable command provenance. It drops the false Make claim by requiring a recognised native task. Python yields better inputs for a review of command bindings. The generic result still supplies the component inventory. Neither result alone captures SOVTrace's full verification or permissions contract.

## Adapter and adoption integration

The command IDs below come from `composition.commands` and match the corresponding `python-native-*` finding by suffix. All are based at scope and cwd `.`, have empty `env` and profiles, and are classified `unexecuted_native_command`. Each carries review preconditions for the invocation, toolchain, environment and side effects.

| Proposed binding for future review | Effective argv | Proposed decision |
| --- | --- | --- |
| `python-test-3a72cf4e1e5bd64c` | `uv run --locked pytest -q` | Withhold approval. Review tests, uv setup and offline isolation. |
| `python-lint-9c1d107b3bfecd5f` | `uv run --locked ruff check .` | Withhold approval. Review locked prerequisites and project-root scope. |
| `python-format-check-36232a38d65c0f5a` | `uv run --locked ruff format --check .` | Withhold approval. Preserve check-only behaviour and locked prerequisites. |
| `python-typecheck-cc54f34583fc3ff7` | `uv run --locked pyright` | Withhold approval. Review Pyright's Node availability and configured Python scope. |

An unapproved recommendation is omitted from initial bindings. The decision schema supports `approve` for current effective commands and `retire` for existing bindings. It has no initial negative-binding record. No adapter input or conflict resolution was proposed. Proposed specialised files are listed below and remain unreviewed.

**Information flow.** `adopt_adapters` rereads the external selection and the separate `adapter-adoption-decisions-v1` document, verifies ADS source/provider identity, recomputes assessment and composition, then records approved command fields and their provenance. `selection_reviewed: true` and per-binding or file `reviewed: true` require human review. Approved file effects use explicit canonical component IDs and exact external content, mode, ownership and contribution/binding references. Plans preserve scope, source and effective argv/cwd/env, profiles, preconditions and provenance. The approved projection is narrower than the full assessment. Keep the evidence and rationale available for review, including SOVTrace's CI guard and checks outside the command bindings. Binding approval creates no Make targets or execution authority. An explicit validation specification is separate. Sources are ADS `docs/adoption-workflow.md`, `schemas/adapter-adoption-decisions-v1.schema.json`, and `lib/agentic_developer_setup/adoption/{adapter_transaction,plan_validator}.rb`.

**Sequencing.** `initial_adoption` handles baseline dispositions and proposes v1 metadata. `adopt_adapters` uses adapter-specific decisions in a separate transaction. It can propose v2 from an empty target or migrate v1 at its recorded ADS revision, preserving component records and scope. Adapter operations reject baseline selection flags, but may include reviewed specialised file effects. An adapter-first transaction followed by `add_components` can preserve adapter metadata while recording previously unrecorded baseline IDs at the recorded source. Keep inactive pilot candidates out of metadata if they may be selected later. An already-recorded inactive ID cannot be reactivated by `add_components` or created as a new adapter specialisation at this revision.

### Non-mutating operations

Prepare `baseline-decisions.yml` externally using `schemas/framework-adoption-decisions-v1.schema.json`. Use six component records. Defer `agent_instructions` and `review_policy` pending review, decline `github_access_helper` because access is separate, and mark `command_interface`, `architecture_scaffold` and `ci_workflow` repository-owned with rationales. Supply equivalents `CONTRIBUTING.md` and `.github/workflows/offline.yml` plus the four command strings shown above for the command interface, `docs/ARCHITECTURE.md` for architecture, and both workflow paths for CI.

```sh
bundle _2.5.22_ exec ruby scripts/manage_adoption.rb plan "$target" \
  --operation initial_adoption --framework-source "$ads" --scope . \
  --component agent_instructions --component review_policy \
  --component command_interface --component architecture_scaffold \
  --component ci_workflow --component github_access_helper \
  --decisions "$out/baseline-decisions.yml" --effective-date 2026-11-22 \
  --output "$out/baseline-plan.yml"
bundle _2.5.22_ exec ruby scripts/manage_adoption.rb preview "$out/baseline-plan.yml"
bundle _2.5.22_ exec ruby scripts/manage_adoption.rb check "$out/baseline-plan.yml"
bundle _2.5.22_ exec ruby scripts/manage_adoption.rb apply "$out/baseline-plan.yml" --dry-run
```

| Operation and inputs | Observed status and conclusion |
| --- | --- |
| Baseline `initial_adoption` and six decisions at the pinned revisions | **0.** No diagnostics or component file writes proposed. Three repository-owned and three inactive records would create v1 metadata only. No validation spec or commands were supplied. |
| Baseline preview, check and dry-run | **0, 0, 0.** Preview showed the proposed metadata diff and applicability. Check and dry-run confirmed applicability at the captured state. Target metadata remained absent. |
| Baseline stale-input check | A second plan using a copy of the decisions passed planning. Appending a YAML comment to that external input and checking the second plan returned **4**, `plan_stale: input identity changed`. Correct protective rejection. |
| `adopt_adapters` with unreviewed selection | **2**, `invalid_document: $.selection_reviewed: must equal true`. No plan output. Correct protective rejection and missing human review. A reviewed plan, preview, check and dry-run are therefore unevaluated for the adapter. |
| `adopt_adapters` with both adapter flags and baseline `--component command_interface` | **2**, `invalid_invocation` with a diagnostic rejecting baseline selection flags. No plan output. Correct operation boundary. |

For the adapter refusal, invoke `plan "$target" --operation adopt_adapters --framework-source "$ads" --scope . --adapter-selection "$out/python-selection.yml" --adapter-decisions "$out/adapter-unreviewed.yml" --effective-date 2026-11-22 --output "$out/adapter-refused-plan.yml"` with the same Bundler command prefix. The deliberate negative control has schema version 1, `selection_reviewed` false and empty `adapter_changes`, `commands`, `components` and `retained_files`. Use a distinct output for the mixed-flags control. The baseline stale control uses distinct decision and plan paths.

Unknown bindings, manager conflicts, mixed scopes, collisions, source drift and revision changes were reviewed through implementation and pinned synthetic tests such as `test/adapters/python_test.rb` and `test/adoption/python_transaction_test.rb`. They were not induced in the pinned target. The successful baseline plan establishes mechanical preservation of native files for those inputs. It establishes no specialised content or v2 applicability for SOVTrace. V2 preflight additionally binds assessment inputs, reviewed bytes and Git identities, and rebuilds the approved projection. Exact-plan acceptance remains a separate authorisation.

## Component inventory and proposed dispositions

Catalogue paths are from ADS `framework.yml`, with source on the left and SOVTrace target on the right. The smallest credible future file set is **`agent_instructions` and `review_policy`**, both proposed `specialised`. The Python adapter can add integration evidence through separately reviewed bindings, without a new runtime or command wrapper. Nothing is proposed for exact inheritance.

| Component | ADS source → target | Proposed disposition and rationale |
| --- | --- | --- |
| `agent_instructions` | `baseline/AGENTS.md` → `AGENTS.md` | **`specialised`.** Absent as a standing agent contract. Point to native docs, clearly distinguish offline from live work, cover private inputs and limit permissions. Low authoring and update cost if it links to authoritative files. Requires owner review and provenance clearance. |
| `review_policy` | `baseline/REVIEW.md` → `REVIEW.md` | **`specialised`.** No standing policy. Existing contribution and issue review rules are the inputs. Specify evidence, review scope, independent review and owner acceptance without automatic paid reviews. Low to moderate review/maintenance cost. Coordinate with `AGENTS.md` and owner policy. |
| `command_interface` | `baseline/Makefile` → `Makefile` | `repository_owned`. Direct documented uv commands and CI are sufficient. A future wrapper would need to delegate exactly and preserve environment differences. Added maintenance has low value today. |
| `development_guide` | `baseline/docs/DEVELOPMENT.md` → `docs/DEVELOPMENT.md` | `repository_owned`. `CONTRIBUTING.md` covers setup, checks and operational constraints. |
| `testing_strategy` | `baseline/docs/TESTING.md` → `docs/TESTING.md` | `repository_owned`. `CONTRIBUTING.md`, offline CI and the case study provide the verification contract. |
| `architecture_scaffold` | `baseline/docs/ARCHITECTURE.md` → `docs/ARCHITECTURE.md` | `repository_owned`. The target path already contains authoritative architecture documentation. |
| `domain_context` | `baseline/docs/DOMAIN.md` → `docs/DOMAIN.md` | `repository_owned`. The existing README and engineering case study cover the supported domain and boundaries. |
| `ci_workflow` | `baseline/.github/workflows/ci.yml` → `.github/workflows/ci.yml` | `repository_owned`. The two native workflows keep offline and networked audit jobs separate. Additional jobs would duplicate coverage. |
| `agent_prompt` | `baseline/docs/AGENT_PROMPT.txt` → `docs/AGENT_PROMPT.txt` | `deferred`. No standing prompt. Use the issue and a short agent contract before introducing another entry point. |
| `code_review_skill` | `baseline/.github/skills/code-review/SKILL.md` → `.github/skills/code-review/SKILL.md` | `deferred`. Absent. Reconsider after the review policy is exercised. Needs a compatible host and agreement on independent review evidence. |
| `agent_launcher` | `baseline/scripts/run_codex.sh` → `scripts/run_codex.sh` | `deferred`. No repo launcher. Potential account and permission consistency comes with shell/runtime checks and mandatory telemetry and usage helper sourcing. The collector is used for the usage path. Review runtime, account isolation, local output and GitHub access as separate requirements. |
| `git_hooks` | `baseline/lefthook.yml` → `lefthook.yml` | `declined`. Absent. Adds local setup and execution cost alongside existing commands and CI. |
| `pull_request_template` | `baseline/.github/PULL_REQUEST_TEMPLATE.md` → `.github/PULL_REQUEST_TEMPLATE.md` | `deferred`. Absent. Existing PR guidance is usable. Revisit if repeated validation or handoff omissions justify a template. |

Other catalogue IDs have these proposed dispositions.

- `deferred` for `commit_metadata`, `agent_ready_issue_template`, `bug_report_issue_template`, `discovery_or_shaping_issue_template` and `issue_template_config`. Existing public issue/PR workflow works without additional standing conventions.
- `declined` for `claude_agent_entrypoint`, `github_access_helper`, `agent_run_telemetry`, `agent_run_usage`, `agent_run_usage_collector` and `agent_run_outcomes`. A second entry point, GitHub credential delegation and run data collection have no demonstrated near-term benefit in this scope. This also rules out the unmodified launcher for the proposed file set.

## Compatibility and security

- Preserve Python 3.12, project compatibility declarations, uv and the committed lock. Verify the chosen Python build exposes the SQLite API used to disable extension loading. Any future binding or wrapper must preserve argv, cwd and verification semantics. `--locked` still allows setup effects. A binding with empty `env` does not carry CI's offline, no-credential and socket guard controls.
- Keep the native smoke checks, demos, synthetic evaluator and fail-closed secret/dependency jobs. Future validation needs reviewed offline prerequisites and credential scrubbing, with networked audit run separately. The Python hook is limited to its protected Python processes.
- In proposed agent instructions, keep GitHub write access and account selection explicitly delegated, forbid live model use for offline verification and keep inputs synthetic. In the review policy, preserve bounded independent review and owner acceptance. Keep workflow permissions, release gates, v0.1.0 and public visibility outside the pilot.
- The generic and adapter CLI paths use static inventory and bounded config reads. The adapter executes Ruby providers from the selected ADS source. Review and revision-pin that code. Transaction checks protect identity, path containment and ownership. Attestation fields and a valid plan ID do not authenticate a reviewer or grant permission to execute a command.

### Native verification for this report

Verification used an external `git archive HEAD` export of the target. With `$python312` set to the local Python 3.12 executable, `uv --offline --no-cache lock --check --python "$python312"` passed. Offline sync without a cache stopped at the Pyright wheel. With existing public package cache entries copied to an external `UV_CACHE_DIR`, editable sync stopped at a build dependency. `uv --offline sync --locked --python "$python312"` succeeded with `UV_NO_EDITABLE=1` and an external `UV_PROJECT_ENVIRONMENT`. The application was installed only in that verification environment.

The local Python 3.12.7 build lacked `sqlite3.Connection.enable_load_extension`. Pytest and the demos and evaluator failed in that environment at the read-only database setup in `src/sovtrace/qa/execution.py`. A Python 3.12.10 build with the required API passed the full offline rerun. With `UV_OFFLINE=1`, `UV_NO_EDITABLE=1`, the offline guard on `PYTHONPATH` and provider variables removed, the four documented `uv run --locked` checks above passed. Pytest reported 1,445 passed, 18 skipped and 68 subtests passed. The same uv prefix passed `sovtrace --help`, `sovtrace --version`, `python scripts/demo.py` in text and JSON, `python scripts/case_study_demo.py` and `python scripts/verify_synthetic.py --offline` with 14 fixtures and 80 passing question outcomes.

The skips cover real Gitleaks and isolated HTTP audit integration cases. No local Gitleaks binary or live advisory check was used. These native checks establish behaviour in the stated export environment. GitHub CI and independent technical review are separate delivery evidence.

## Licensing and provenance

At the pinned ADS revision, the tracked tree contained no root or candidate-specific `LICENSE`, `COPYING`, notice file or identified grant covering redistribution and modification of the baseline artifacts. Searches covered tracked paths and licensing/attribution references in root and component documentation. This did not establish the rights of all contributors or applicable third-party notices. The resolved Ruby tools used to assess the repository are separate from any material proposed for distribution.

SOVTrace declares MIT in `LICENSE` and `pyproject.toml`; `CONTRIBUTING.md` requires contributors to have rights to submit under those terms. Public availability and common ownership supply no documented permission for the proposed reuse. Before any public incorporation, establish an applicable grant and rights to modify and redistribute the selected files, review third-party obligations and record any required attribution. Alternatively, separately review the provenance and rights of independently authored repository material before proposing it. This report includes no baseline file content.

For a later approved adoption, retain framework revision/version, stable component IDs, ownership, source and reviewed-content digests, modes and adapter manifest/provider digests in the applicable ADS records and review evidence. Preserve scope, binding provenance and reasons in the implementation review. These support traceability. Rights and permission require their own record.

## Framework findings

The following are proposed ADS follow-up descriptions at `bf51905d77f892cfcc9c6f8784214ce9a1e9548b`. They can be evaluated independently. No follow-up issues were opened.

| Finding | Reproduction, expected and observed | Classification, impact and proposed resolution |
| --- | --- | --- |
| Prose becomes a Make command | Run generic assessment and inspect E012 and `tooling.command_surface`. `docs/ARCHITECTURE.md` contains prose but no command declaration. Expected no command. Observed `make estimates`. | **Confirmed implementation defect, medium.** Can suggest a nonexistent developer command. Identify fenced shell or exact task evidence, and add a prose regression fixture. Does not block a manually reviewed pilot. |
| Generic static typing misses Pyright | Run generic assessment against `[tool.pyright]`, `pyright[nodejs]` and the local and CI invocation. Expected a static type-checking signal. Observed an empty type-checker list and `not_detected`. Python correctly found it. | **Potential implementation gap, medium.** Generic documented coverage is a subset. Confirm whether Pyright should be first-class or the output should signal partial coverage. Add explicit evidence or make the scope visible. No independent adoption blocker. |
| Native documentation triggers replacement suggestions | Compare `component_recommendations` with `docs/ARCHITECTURE.md`, `CONTRIBUTING.md` and the case study. Expected preservation or manual equivalence review. Observed architecture specialisation and test/domain adoption recommendations based on path heuristics. | **Usability gap, medium.** Can lead to redundant files. Distinguish path resemblance and an established repository-owned equivalent and expose uncertainty in recommendations. Manual dispositions contain the impact. |
| Incomplete CI inspection yields missing-command findings | Compare `python-ci-missing-*` with `.github/workflows/offline.yml`. In an external fixture with a minimal uv project and documented pytest command, a literal one-line CI step yielded `ci-alignment`. The same invocation after `unset` in a multiline step with a guard-path expression yielded `ci-dynamic` and `ci-missing`. | **Intentional support limitation with a diagnostic usability defect, medium.** Expected incomplete/unknown coverage for skipped steps. Report the coverage as unknown or explain the skipped step at the missing finding. Keep shell evaluation out of assessment. Manual CI comparison required before binding review. |
| Optional live examples suppress all advisory profiles | See `python-risk-credentials` from the optional live example in `README.md`. In the literal synthetic fixture above, adding a fenced placeholder API-key assignment to the README kept the pytest argv but changed its profile list from `standard` to empty. SOVTrace's offline command does not require a live key. | **Possible future enhancement, low.** Conservative global suppression follows `restricted_profiles?`. Preserve the warning and distinguish credential-sensitive surfaces from offline command evidence, or expose the reason for suppression directly. No independent adoption blocker. |
| Assessment documentation still describes a pre-provider state | Compare the “Independent adapter assessment” section of `docs/repository-assessment.md` with `framework.yml`, the supported manifests and `docs/adoption-workflow.md`. Expected an accurate versioned description. The generic guide says all production adapters remain planned. | **Confirmed documentation defect, low.** Correct the support statement and link to the reviewed v2 transaction. The separate assessment and v1 boundaries still apply. |

The review-attestation, mixed-flags and stale-input refusals are protective evidence. Missing licensing permission and owner review are prerequisites, and incomplete local tooling is an environmental limitation.

## Engineering value and maintenance

The public framework evidence already has value. An established application exercised generic and Python discovery, exact command provenance, native ownership and protective planning boundaries. A reviewed installation would extend the evidence to metadata migration or creation, managed files and post-installation verification. Future SOVTrace engineering could gain a short repository-specific agent contract and more consistent handoff. Existing uv and CI infrastructure already handles execution and feedback.

Once rights and review prerequisites are resolved, drafting two short specialisations, checking bindings and running existing regression checks are roughly a half day to one working day of combined implementation and review effort, excluding licensing resolution and external CI latency. Maintenance includes reviewing authority guidance when commands or policies change, reviewing provider/digest changes at each proposed framework update, and checking ownership metadata for drift. A launcher, hooks, extra CI, runtime accounts or telemetry would add configuration and ongoing cost. The limited candidate has no additional CI jobs, hook execution or credential dependencies.

## Adoption recommendation

**DEFER** public installation until redistribution and attribution permissions are clear, an owner reviews the file and binding scope, and a reviewed v2 plan can be checked with SOVTrace's offline and security boundaries accounted for. The demonstrated static and baseline planning value justifies retaining a minimal candidate. It does not yet justify taking on the rights, maintenance and operational commitments. The temporary pause in feature work is independent of that decision.

## Proposed next stage

1. Obtain a separate owner decision on rights and scope. Review `AGENTS.md`, `REVIEW.md` and the proposed Python 1.0.0 selection and bindings. Decide whether v2 should start without bindings or include individually reviewed commands with an independently approved verification environment.
2. In a separately authorised implementation, use the documented v2 path from a clean target and preserve native files. Preview, check and dry-run the actual reviewed plan. Identify managed paths, metadata and recovery conditions before approval. Retain the previous reviewed Git state and use the documented recovery procedure for partial transactions. Validate post-installation ownership, exact file content and bindings with the native checks, CI and `inspect_adoption.rb`. Keep installation evidence separate from operational evidence.
3. Use a genuine future engineering task only when independently prioritised. The case study identifies broader independent interpretation evaluation as future work. A bounded extension of the existing credential-free synthetic evaluation coverage against an independently specified gap would exercise session preparation, agent guidance, locked commands, limited permissions, review handoff and existing CI. No such task is currently scheduled. Record the task and starting revisions, components actually used, setup friction, interventions, check outcomes and any ADS defects. Report observed workflow changes and limitations without causal productivity claims.

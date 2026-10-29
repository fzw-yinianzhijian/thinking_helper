# Start here

Read README.md, docs/PRODUCT.md, docs/ARCHITECTURE.md, docs/HANDOFF.md, then docs/STATUS.md. The current request is to build a small persistent thinking partner, not a collection of chat features.

## Product invariants

- Work continues without an open browser. Goals, topic versions, job ownership and updates survive restart.
- Every research result relates to a specific topic version. Old work cannot overwrite a user's correction.
- New findings change a reusable structure with provenance. A new summary alone is not progress.
- The user may speak freely. Preserve uncertainty and alternatives; never polish a guess into established evidence.
- Voice is an interface to shared state. Stopping speech does not implicitly cancel background research.
- Distinguish content progress from changes to the system's methods. Runtime code does not edit itself.
- Prefer four clear responsibilities: interaction, workspace, background work, review/integration.

## Development

Python >=3.10. Run `python -m unittest discover -s tests -v`; start with `python -m uvicorn app.main:app --host 127.0.0.1 --port 8890`.
Keep credentials outside Git and browser responses. Do not log provider request headers. Bind to loopback; use an SSH tunnel for the remote demo.
Keep deterministic tests separate from opt-in paid smoke checks. Record actual checks and limitations in docs/STATUS.md.
Do not mark voice quality, autonomous improvement, or long-term usefulness verified from unit tests.

## Iteration contract

Each change names the observed problem, affected module, expected improvement, validation and rollback. Keep handoff usable without this chat. Update the live status instead of appending competing plans.

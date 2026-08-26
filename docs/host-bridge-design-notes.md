# Host Bridge Design Notes

This implementation intentionally separates **where evidence must come from** from **which coding assistant happens to be used**.

The workstation is treated as a bounded scientific instrument:

- GitHub/ChatGPT can author repository code and request a fixed verification profile.
- The local worker decides whether the exact SHA is eligible.
- The local worker owns paths, credentials, media, GPU access, process invocation, and result sanitization.
- The Sheet carries only immutable request identity plus compact receipts.

This mirrors the proven patterns from Nightmare (exact-SHA detached verification) and Pancake (bounded remote-to-local commands), but keeps Shock's Art-specific profiles and lifecycle independent.

The first three profiles target the remaining pure workstation blockers rather than trying to generalize arbitrary local automation prematurely.

# Security

HeatShield handles phone numbers (MSISDNs), presence evidence about named workers, and operator
credentials. Please report anything that could expose them privately, not in a public issue.

**Report to:** balcihkutsi@gmail.com. Please include what you found, how to reproduce it, and what an
attacker would gain.

You can expect an acknowledgement within a few days.

## Scope

In scope: this repository's code and the public demo at https://heatshield-demo.onrender.com.

The public demo runs in fixture mode with a **fictional** roster (every number is in the non-assignable
`+99999…` range), so reading its data is expected and not a finding. Please do not load-test it; it
is a free instance.

## What the code already guarantees, so you can check it

- **No default secrets.** `NAC_MODE=live` refuses to start without `HS_API_TOKEN`, `WEBHOOK_TOKEN` and
  `PHONE_HASH_SALT`. Tokens are compared in constant time.
- **Writes and personal-data reads need `HS_API_TOKEN`.** They are only opened in demo mode, which
  itself is refused against the live operator.
- **Phone numbers are masked.** Raw numbers never appear in a response or a log (`_mask_deep`,
  `MaskingFilter`), and are stored as HMAC-SHA256 hashes.
- **Coordinates are gated.** A coordinate is requested only after an escalation, once, and coarsened
  to three decimals before it is stored.
- **Tools never call a live API by accident.** The live probes in `tools/` require an explicit
  `--live` or `--mode live`.

The tests for these are in `tests/test_security.py` and `tests/test_tools.py`.

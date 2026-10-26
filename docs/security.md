# Security

## Current milestone

The base Compose stack is paper-only and does not mount a wallet key. Live execution must be added
as a separate, explicit configuration after paper and reconciliation gates pass.

## Wallet policy

- Never use a primary savings wallet.
- Use a dedicated hot wallet funded only with the approved loss budget.
- Never paste a key into an issue, chat, log, dashboard or model prompt.
- Never store a key in Git, an image layer, Compose YAML or environment example.
- Paper mode must start successfully with no key available.

## Future local secret mount

For an eventual live profile, keep the source file outside the repository under the WSL Linux
filesystem with mode `0600`, and mount it read-only at `/run/secrets/...` only in the `trader`
container. Standalone Compose secrets improve delivery but are not an encrypted vault; host file
permissions and disk security still matter.

## Container policy

The trader runs as a non-root user with a read-only root filesystem, dropped Linux capabilities,
no Docker socket, bounded temporary filesystems and only the required persistent data mount.
PostgreSQL and Prometheus are not published to the Windows host. Grafana binds to loopback only.

## Live promotion gates

Live mode remains unavailable until the system demonstrates restart reconciliation, duplicate
submission prevention, partial-fill handling, stale-feed shutdown, database failure shutdown,
daily-loss halt and a tested emergency cancel path.


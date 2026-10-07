# Moving from an existing folder installation

Do not replace scripts or guest tools while another session is using them. This package can live beside an existing installation and use a unique VM name/state directory until those sessions finish.

1. Clone the new repository into its own directory. Build its tools and run `doctor`.
2. Choose a stopped clean source without personal data. Run `setup --from SOURCE` with a new working VM name and a new clean-image name in configuration. Do not replace the old source or its working VMs.
3. Test installation, input, inspection and capture on the new VM. Existing guests retain their old controller until explicitly prepared with the new tools.
4. When all users of the old installation finish, update their executable path and environment/configuration to point at this repository. Choose a private VM name per concurrent session.
5. Keep the old folder and clean image for rollback until the replacement is verified. Retire old VMs only after checking no session owns or needs them.

The new package has no implicit Lamp default. Choose `config/lamp.json` or pass the app name explicitly. New runtime state is stored separately, and build output is not part of the repository. A source-only checkout cannot replace an existing VM disk; the setup command prepares a new guest controller and permissions.

## Switching to durable slot waiting

Drain the current installation first: let each current session stop/destroy its own
clone and finish or cancel its old waiter. Do not kill or convert another session's
VM/waiter. Merge the reviewed branch only after the shared checkout is no longer
in use, then have all sessions use its updated `bin/lampvm` with the existing
`LAMPVM_CONFIG` and common `LAMPVM_STATE_DIR`. Keep a copy/ref of the previous
source for rollback. Never run old and new waiters together expecting FIFO order.

This change adds Python slot scheduling/observations; the guest helper and Tart
bundle do not change. Existing ownership records/config/clean images are retained.
`slots.json`, `slots.lock`, worker locks and ticket logs are created only under
runtime state on use. Nothing is adopted automatically: old running VMs remain
visible as untracked holders until a new managed start. There is no retroactive
start time, queue age or activity history for the old processes.

For rollback, let updated sessions stop their own clones and cancel their own
queued tickets before restoring the previous source. Detached workers execute
from the checkout that launched them: do not replace that checkout under active
workers. Confirm via `status --json` that managed requests are terminal; check its
worker PIDs with `ps -p PID` to confirm they have exited. Saved slot records can stay outside git for inspection; old
code ignores them. No VM reset, image download, guest reinstall or tool build is
needed specifically for this scheduler change.

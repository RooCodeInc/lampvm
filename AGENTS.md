# Working on lampvm

This is a command-line tool for agents that test macOS apps in disposable Tart VMs.
Read README.md before installing it. The host screen and the user's applications are not test surfaces.

- Never operate another session's VM. Use a private named clone and release only that clone.
- Wait for a slot with `start --wait SECONDS --session TASK_OR_THREAD_ID` and no `--detach`: it returns when the VM is ready for commands, so nothing polls `status`. Use the same session id on every start. If the shell cuts it off, rerun the same line.
- Keep the VM running under its hold through a rebuild or any pause of a few minutes, and reinstall the new build into it.
- `stop` or `destroy` only when you are done or away for long: they give the slot up, and `start` after `stop` joins the back of the line.
- `reset --yes` only when you need a clean guest: it keeps the slot and leaves the VM running, so `stop` or `destroy` afterwards.
- A person can watch or use any running VM with `bin/lampvm view [NAME]`. Offer that command; do not run it for them unprompted, since it opens a window on their screen. Expect shared pointer/keyboard while it is open.
- Keep credentials, personal app data, signing keys, VM disks, logs, screenshots and recordings out of git.
- Do not replace owned VMs manually with Tart: ownership records must describe the same VM throughout its lifetime.
- Keep runtime state outside this checkout. Setup configuration templates are read-only inputs.
- Changes to input or capture need real guest evidence using the offline harness, in addition to host tests.
- Build the guest helper for macOS 14; build lockstate for the installing host's macOS.
- Preserve Tart's complete signed bundle and licence. macOS images are separate downloads, never repository assets.
- Never open a GitHub issue for this personal workflow. Track follow-ups locally.
- No attribution lines in commits or pull request descriptions.

Run `python3 -m unittest discover -s tests -v` and `bin/lampvm build` for host checks.
Use `tests/smoke.py --help` for isolated real-VM proof. Store proof outside the repository.

# Moving from an existing folder installation

Do not replace scripts or guest tools while another session is using them. This package can live beside an existing installation and use a unique VM name/state directory until those sessions finish.

1. Clone the new repository into its own directory. Build its tools and run `doctor`.
2. Choose a stopped clean source without personal data. Run `setup --from SOURCE` with a new working VM name and a new clean-image name in configuration. Do not replace the old source or its working VMs.
3. Test installation, input, inspection and capture on the new VM. Existing guests retain their old controller until explicitly prepared with the new tools.
4. When all users of the old installation finish, update their executable path and environment/configuration to point at this repository. Choose a private VM name per concurrent session.
5. Keep the old folder and clean image for rollback until the replacement is verified. Retire old VMs only after checking no session owns or needs them.

The new package has no implicit Lamp default. Choose `config/lamp.json` or pass the app name explicitly. New runtime state is stored separately, and build output is not part of the repository. A source-only checkout cannot replace an existing VM disk; the setup command prepares a new guest controller and permissions.

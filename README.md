# lampvm

A command-line toolkit for agents that test Mac apps inside a disposable macOS VM. It runs without a VM window on the host, so the host can stay locked or be used by someone else. Input, Accessibility inspection, screenshots and recording happen in the guest. No host Accessibility or Screen Recording permission is needed.

## Install from a clean Mac

Requirements: Apple Silicon, host macOS 14 or later, Xcode Command Line Tools (`xcrun swiftc`, `codesign`, `git`), Python 3 and internet access. Install the Command Line Tools if needed with `xcode-select --install`; that command presents a host installer which a person may need to complete. Start with roughly 40 GB of available storage. The default VM uses 4 CPUs and 8 GB RAM; allow more memory for the host. Apple's macOS virtualization limit is two running macOS guests per Mac. On recent macOS releases, Tart may require the host login Keychain to be unlocked when it starts; a locked screen and a locked Keychain are different conditions.

Clone this private repository using host GitHub access. The example uses an authenticated GitHub CLI (`gh`); ordinary `git clone` works too. Then run:

```sh
gh repo clone RooCodeInc/lampvm
cd lampvm
bin/lampvm setup
bin/lampvm doctor
bin/lampvm start
bin/lampvm exec /usr/bin/sw_vers
```

Setup downloads pinned Tart 2.32.1, preserves its signed application bundle, verifies its checksum and signature, builds the guest controller and a host lock-state helper, downloads a public macOS base image pinned by OCI digest and prepares a working VM plus a stopped clean snapshot. It never downloads or includes another user's VM. First setup can take a long time because the image is large. Setup options include `--image OCI_REFERENCE`, `--no-download` to use an existing Tart, and `--wait-slot SECONDS` to wait without stopping anyone else. Use `python3 lib/setup.py --help` to list preparation options. `--from EXISTING_STOPPED_VM` uses a local clean image instead of downloading one; only use a source you know contains no private data.

Setup and first start finish the pinned image's known English post-update pages through observable controls: defer updates, skip Apple Account sign-in and decline FileVault. The final welcome button uses an explicit Accessibility press because macOS exposes its mouse hit only as the application. Completion requires the assistant to disappear; unfamiliar pages stop with inspection instructions.

The public base image is configured for automation, with user/password `admin`/`admin`, automatic login, passwordless sudo and SIP disabled. It includes Tart's guest agent. This is an isolated test desktop, not a place for real credentials or personal work. Setup prepares guest automation access. macOS can still present unfamiliar first-boot/update or screen capture consent prompts; read `doctor` output and a screenshot if a capture or Accessibility request fails. Confirm the app works before treating setup success as a test result.

Runtime files live under `~/Library/Application Support/lampvm`; VMs and downloaded image cache live under Tart's own `~/.tart`. Compiled tools, logs, screenshots, recordings and VM disks stay outside git. `bin/lampvm build` rebuilds the tools; the lock-state helper targets the host's macOS version rather than one developer's machine.

## Give each agent its own VM

A guest has one keyboard, pointer, focus and desktop. Concurrent commands into the same guest can interfere. Use a unique VM name per agent, coordinate the two available running slots, and never stop or reset another session's VM.

```sh
L=./bin/lampvm
$L clone test-my-session
$L --name test-my-session start
$L --name test-my-session install /path/to/Test.app
$L --name test-my-session launch Test
$L --name test-my-session tree Test
# Do the test, then release only your VM.
$L --name test-my-session stop
$L --name test-my-session destroy --yes
```

`clone` uses the configured clean VM; `clone NAME --from STOPPED_VM` selects another clean source. Cloning shares disk blocks initially, but changes consume storage. `destroy --yes` deletes only the selected owned working VM. Clean-image ownership records are protected from start, destroy and reset. Use `reset --yes` only on your working VM; it replaces that VM from its recorded stopped clean source. Do not select the clean image for destructive commands.

## Drive an app

Apps are explicit by default. Set `LAMPVM_NAME` to select your private guest without repeating `--name`. A command accepting `[APP]` requires either an app argument or a configured default app. Use an installed app's process name, usually its bundle name without `.app`.

```sh
export LAMPVM_NAME=test-my-session
L=./bin/lampvm
$L launch Test
$L tree Test --json
$L clickon Test 'Continue' --exact
$L type 'made-up input'
$L key return
$L wait Test 'Ready' --exact --timeout 10
$L assert Test 'Ready' --exact
$L shot /tmp/test-ready.png --app Test
$L record /tmp/test-flow.mov --seconds 5 --app Test
```

Recording blocks until it finishes; start it in a separate shell/background process to capture input commands. Cropping uses the app window, not guessed screenshot coordinates. Screenshots use a unique guest temporary filename on every call; omitting the host destination also gives a unique output name. Concurrent captures do not overwrite each other's temporary files, but concurrent UI interactions still require separate VMs.

Coordinates use screen points with the origin at the top left. They are not screenshot pixels: Retina screenshots may have twice as many pixels. Read `tree`, `find` or `bounds` before clicking. Prefer exact labels and verify observable state afterwards. A successful click hit report confirms the target was under the pointer; it cannot prove the app completed its action.

## Commands

Global selection: `--name NAME`, `--config FILE`, `--state-dir DIRECTORY`. Environment equivalents: `LAMPVM_NAME`, `LAMPVM_CONFIG`, `LAMPVM_STATE_DIR`; `LAMPVM_APP` sets a default target app; `LAMPVM_TART` selects an existing Tart executable. See `help` for exact argument order.

| Command | What it does |
|---|---|
| `setup` | Downloads dependencies, builds tools and prepares the guest and clean image. `--from VM` uses a stopped local source. |
| `build` | Builds the host lock-state and guest input/inspection helpers into external state. |
| `doctor` | Checks host dependencies, Accessibility and reported guest health, including unfinished Setup Assistant. It does not prove video consent or every interaction. |
| `start [--wait SECONDS]` | Starts the selected VM without a host window, audio or shared clipboard, and waits for its desktop. Waits for a free slot if requested. |
| `stop` | Shuts down the selected VM, then forces it off if graceful shutdown times out. |
| `status` | Reports VM state/configuration and host lock state. |
| `apps` | Lists running guest apps with process names, bundle IDs, PIDs and frontmost status. |
| `ocr [--app APP]` | Reads visible text with offline Apple Vision OCR, including confidence and text boxes in image pixels. App captures include `screenOriginPoints` and `pixelsPerPoint` conversion; boxes have a top-left image origin. |
| `clone NAME [--from VM]` | Creates a separate working VM from a stopped clean source. |
| `destroy --yes` | Stops and deletes the selected owned working VM; clean images are protected. |
| `reset --yes` | Replaces the selected working VM with a fresh clone of the configured clean image. |
| `install PATH` | Copies an app to guest `/Applications`, quits the previous copy and clears copied extended attributes. |
| `launch [APP] [ARGS…]` | Opens an app, passing optional launch arguments. |
| `quit [APP]` | Requests that an app quit. |
| `activate [APP]` | Brings the app forward. |
| `wipe --yes` | Quits the configured app and clears only explicitly configured preference domains/data paths. It does not erase the guest or Keychain. |
| `tree [APP] [--json]` | Reads the app's Accessibility tree: roles, frames, labels and values. |
| `find [APP] TEXT [--json] [--exact] [--role ROLE]` | Finds labelled Accessibility elements; role restricts matches (for example `AXButton`). |
| `windows [APP]` / `bounds [APP]` | Lists app windows / reports an app window's screen rectangle. |
| `clickon [APP] TEXT` | Activates the app, finds an element and posts a real click, then reports the target/hit result as JSON. Use `--exact`, `--role ROLE`, zero-based `--index N`, or `--right`. |
| `press [APP] TEXT` | Performs the element's Accessibility press action, with `--exact`, `--role` and `--index`. This is a semantic action; verify the resulting app state. |
| `hit X Y` | Inspects the Accessibility element under a point and its parents, including role, label, bounds and process ID. It posts no input. |
| `click X Y` / `doubleclick X Y` | Posts left clicks at screen coordinates. |
| `rightclick X Y` / `move X Y` | Posts a right click / moves the pointer. |
| `scroll X Y DY [DX]` | Positions the pointer and posts wheel scrolling; positive DY scrolls up. |
| `drag X1 Y1 X2 Y2 [SECONDS]` | Holds the left button and moves from source to destination over the specified duration. |
| `type TEXT` | Posts Unicode keyboard events into the focused control; newlines press Return. |
| `key NAME [MODIFIERS]` | Posts supported keys with comma-separated `cmd,shift,alt,ctrl` modifiers. |
| `setvalue [APP] TEXT VALUE [--index N] [--role ROLE]` | Sets a writable Accessibility value directly; useful for controls where key input is unsuitable. This does not simulate typing. |
| `wait [APP] TEXT [--timeout SEC]` | Polls for an element; `--absent` waits for removal, `--exact` and `--role ROLE` restrict matching. A timeout exits unsuccessfully. |
| `assert [APP] TEXT` | Immediately checks presence; `--absent` checks absence and `--exact` and `--role ROLE` restrict matching. Failure exits unsuccessfully. |
| `shot [FILE] [--app APP]` | Captures the desktop, or activates/crops to the selected/configured app window, and copies the PNG to the host. |
| `record [FILE] [--seconds N] [--app APP]` | Records 1–300 seconds (default 5) on the guest and copies the movie to the host; optional selected/configured app-window crop. |
| `exec COMMAND [ARGS…]` | Runs an executable as the guest's logged-in user. |
| `sh` | Runs stdin as a guest zsh script. |
| `push SOURCE DEST` | Streams a host file/folder into a guest directory. |
| `pull SOURCE DEST` | Copies one guest file to the host. |
| `help` | Lists commands and options. |

`key` supports letters, digits, common US-layout punctuation, Return/Enter, Tab, Space, Backspace/Delete, ForwardDelete, Escape, arrows, Home/End, PageUp/PageDown and F1–F12. `type` posts Unicode rather than mapping characters through a physical keyboard layout; tabs and newlines send Tab and Return. Coordinate input commands, `type` and `key` also accept `--app APP` to activate the app first.

The guest helper has two low-level diagnostics in addition to the wrapper commands: `vmctl trusted` reports Accessibility trust, and `vmctl display [WIDTH HEIGHT]` lists display modes or changes the display. The installed path is `$HOME/.local/bin/vmctl` in the guest; inspect/use it through `exec` or `sh`, rather than assuming a guest username.

## Configuration, including Lamp

The default is app-agnostic. JSON configuration supplies `vm`, `clean`, `image`, `cpu`, `memory`, `display`, optional `app`, and optional `wipe.preference_domains` / `wipe.paths`. `--config FILE` or `LAMPVM_CONFIG` merges a template over runtime state `config.json`. Templates are read-only: setup records effective choices in runtime state, without rewriting the template. Use unique names/state directories for independent setups. Paths used by `wipe` are relative to the guest home and limited to app-data directories under `Library/Application Support/` or `Applications/`. Keep your local configs outside git if they contain private details. Review any wipe configuration before running it.

An opt-in Lamp example is provided in `config/lamp.json`. Its bundle ID, preference domains and application data paths preserve Lamp's existing Forge identifiers. Select it explicitly:

```sh
bin/lampvm --config config/lamp.json --name test-my-session launch
```

## Limits and security

- The toolkit uses Tart's local control socket/guest agent to execute commands, CoreGraphics events for mouse/keyboard input, Accessibility for inspection, and guest screen capture for images/video. It does not need host screen access or SSH keys.
- The guest has normal network access. No shared folders or clipboard are enabled, but `exec`, `sh`, `push` and apps can deliberately transfer data. Isolation is not a credentials policy: agents must avoid real sign-ins, keys, tokens, personal data and production accounts.
- Accessibility is only as complete as the app makes it. Canvas/custom controls may expose no labels. Duplicate labels need explicit selection. Popovers, menus, multiple windows and overlays can affect hits. Use screenshots and state assertions; a click report alone is not action verification.
- OCR can help with unlabelled controls, but recognition is approximate. Its boxes refer to the captured image, not screen points; apply the reported crop origin and scale before clicking (`screenX = originX + imageX / scaleX`, likewise for Y). Recordings keep the crop fixed at their start, so keep the app window in place.
- Typing requires focus. Direct value-setting can be unsupported or behave differently from a user's keystrokes. Presence in the Accessibility tree does not always mean an element is visibly on screen.
- The guest's macOS version determines what you can test. A newer host does not make guest-only behaviour match it. Setup defaults to a pinned image digest; if you choose a mutable `latest` tag, it can change. Record `sw_vers` with proof and configure a stable image reference for repeatable runs.
- Audio/microphone and sign-in flows are outside the default setup. Screen capture consent can recur. Host restart does not automatically restart the VM. SIP-disabled test guests should not hold valuable data.
- Setup preserves a VM's hardware identity on retries and refuses to create a clean snapshot after new kernel panic reports or a visible restart-error alert. Start/doctor also check for reported guest health problems. OS/virtualization errors need inspection or a different base image; the toolkit does not dismiss those alerts automatically.
- macOS/Tart licensing applies separately from this toolkit. Source is MIT licensed; Tart is downloaded separately under its own Fair Source licence. See [licence notices](licenses/NOTICE.md). Do not distribute macOS VM disks or private prepared images with the repository.
- Ownership records are local safeguards. Do not delete and recreate toolkit VMs directly with Tart under the same names, or share a state directory with untrusted processes.

## Verify toolkit changes

`tests/build-harness.sh` builds an offline, ad hoc signed AppKit app under ignored `build/`. It provides a click counter, text field, delayed/hidden element, 60 scrolling rows, a drag source/drop target and a right-click context menu. Install it into a disposable private clone, test the commands through the actual guest, and capture cropped proof. No personal app or account is needed. The visible scroll-offset label verifies that wheel input moved the viewport even when Accessibility exposes offscreen rows.

Run host-side tests with `python3 -m unittest discover tests`. These check wrapper behaviour without substituting for VM tests. `tests/smoke.py` drives the harness through a running private VM and saves command output plus cropped screenshots/recording; read its help before running it. Start only your own prepared VM, set its name/state explicitly and delete that clone after proof.

Keep proof, compiled apps, logs and recordings outside the repository. Read the migration notes in [docs/MIGRATION.md](docs/MIGRATION.md) before replacing an existing installation that other sessions use.

# lampvm

A command-line toolkit for agents that test Mac apps inside a disposable macOS VM. It runs without a VM window on the host unless a person asks for one with `view`, so the host can stay locked or be used by someone else. Input, Accessibility inspection, screenshots and recording happen in the guest. No host Accessibility or Screen Recording permission is needed.

## Watch or use the VM yourself

Agents drive the VM, but a person can open it too:

```sh
bin/lampvm view
```

This opens the running VM in a Screen Sharing window on this Mac, with your mouse and keyboard working inside it. If Screen Sharing asks for a type, keep Standard and click Continue. With more than one VM running it lists them; pick one with `bin/lampvm view NAME` (`bin/lampvm status` shows who holds each). Close the window when done: the VM keeps running, and its holder, queue place and idle tracking are untouched. You share one pointer and keyboard with the agent, so watch while it works and take over when it is waiting. Input by hand does not count as an agent command for idle flags.

## Install from a clean Mac

Requirements: Apple Silicon, host macOS 14 or later, Xcode Command Line Tools (`xcrun swiftc`, `codesign`, `git`), Python 3 and internet access. Install the Command Line Tools if needed with `xcode-select --install`; that command presents a host installer which a person may need to complete. Start with roughly 40 GB of available storage. The default VM uses 4 CPUs and 8 GB RAM; allow more memory for the host. Apple's macOS virtualization limit is two running macOS guests per Mac. On recent macOS releases, Tart may require the host login Keychain to be unlocked when it starts; a locked screen and a locked Keychain are different conditions.

Clone the repository, then run setup:

```sh
git clone https://github.com/RooCodeInc/lampvm.git
cd lampvm
bin/lampvm setup
bin/lampvm doctor
bin/lampvm start
bin/lampvm exec /usr/bin/sw_vers
```

Setup downloads pinned Tart 2.32.1, preserves its signed application bundle, verifies its checksum and signature, builds the guest controller and a host lock-state helper, downloads a public macOS base image pinned by OCI digest and prepares a working VM plus a stopped clean snapshot. It never downloads or includes another user's VM. First setup can take a long time because the image is large. Setup options include `--image OCI_REFERENCE`, `--no-download` to use an existing Tart, and `--wait-slot SECONDS` to wait without stopping anyone else. Use `python3 lib/setup.py --help` to list preparation options. `--from EXISTING_STOPPED_VM` uses a local clean image instead of downloading one; only use a source you know contains no private data.

Setup and first start finish the pinned image's known English post-update pages through observable controls: defer updates, skip Apple Account sign-in and decline FileVault. The final welcome button uses an explicit Accessibility press because macOS exposes its mouse hit only as the application. Completion requires the assistant to disappear; unfamiliar pages stop with inspection instructions.

The public base image is configured for automation, with user/password `admin`/`admin`, automatic login, passwordless sudo and SIP disabled. It includes Tart's guest agent. This is an isolated test desktop, not a place for real credentials or personal work; real sign-ins go in an [accounts VM](#sign-in-once-and-hand-the-vm-to-one-agent). Setup prepares guest automation access. macOS can still present unfamiliar first-boot/update or screen capture consent prompts; read `doctor` output and a screenshot if a capture or Accessibility request fails. Confirm the app works before treating setup success as a test result.

Runtime files live under `~/Library/Application Support/lampvm`; VMs and downloaded image cache live under Tart's own `~/.tart`. Compiled tools, logs, screenshots, recordings and VM disks stay outside git. `bin/lampvm build` rebuilds the tools; the lock-state helper targets the host's macOS version rather than one developer's machine.

## Give each agent its own VM

A guest has one keyboard, pointer, focus and desktop. Concurrent commands into the same guest can interfere. Use a unique VM name per agent, coordinate the two available running slots, and never stop or reset another session's VM.

```sh
L=./bin/lampvm
$L clone test-my-session
$L --name test-my-session start   # returns when the VM is ready for commands
$L --name test-my-session install /path/to/Test.app
$L --name test-my-session launch Test
$L --name test-my-session tree Test
# Do the test, then release only your VM.
$L --name test-my-session stop
$L --name test-my-session destroy --yes
```

`clone` uses the configured clean VM; `clone NAME --from STOPPED_VM` selects another clean source. Cloning shares disk blocks initially, but changes consume storage. `destroy --yes` deletes only the selected owned working VM. Clean-image ownership records are protected from start, destroy and reset. Use `reset --yes` only on your working VM; it replaces that VM from its recorded stopped clean source, and a running VM comes back running in the slot it held. Do not select the clean image for destructive commands.

## Sign in once and hand the VM to one agent

On-screen work that needs real accounts (a web app you are signed in to, a Mac app with your login) goes in an accounts VM instead of on your own screen. It keeps your sign-ins between runs, and only you open it: its guest has a password only you know, no automatic login, no SSH and no `sudo` without that password, and the guest agent that answers lampvm and `tart exec` runs only once you have signed in. Agents use it only while you have handed it to their session.

```sh
L=./bin/lampvm
$L accounts create my-accounts                      # at a terminal: choose the guest password
$L --name my-accounts start --wait 3600             # comes up at its login window
$L view my-accounts                                 # sign in as admin, then to the accounts you choose
$L accounts grant my-accounts --session t_abc123    # at a terminal, with your password: hand it to that one session
$L accounts revoke my-accounts                      # stops it; it opens again only when you sign in
```

- `accounts create NAME` clones the clean image (or `--from` another clean source), asks you at the terminal for the guest password twice, waits for a slot and boots it once to lock it down, then stops it. The password goes to the guest on standard input and is not stored on this Mac. It also unlocks the guest's login keychain. Run it yourself, at a terminal. A name that another VM still lists as its clean source is refused, so no `reset` can copy the accounts VM later.
- `start` returns once the login window is up and prints the next step. Nothing can use the guest until you sign in through `view`: Screen Sharing asks for the password, and so does the guest's login window. Closing the `view` window changes nothing.
- `accounts grant NAME --session ID` hands the signed-in VM to the session with that task or thread id. It asks for the guest password and the guest checks it, so an agent cannot hand the VM to itself. Every guest command (`exec`, `sh`, `shot`, `clickon`, `push`, `pull`, recording, `hold` and the rest) then works only with `LAMPVM_SESSION` set to that id; any other session is refused with the name of the session holding it. Granting to another session ends the first grant.
- The grant ends when the VM stops or restarts for any reason: `accounts revoke`, `stop`, a shutdown or a restart from inside the guest (the grant records the guest's boot time). A restarted VM is back at its login window, so handing it over again means signing in again. `accounts revoke` does not wait for the granted session's running commands; stopping the VM ends them.
- Nothing copies an accounts VM: `clone --from`, `setup --from` and spec images refuse it, and `reset`, `destroy` and `up` do not touch it. `accounts destroy NAME --yes` deletes it with its sign-ins; like `grant`, it needs the VM signed in and asks for the guest password. `accounts list` and `status` show each accounts VM and whom it is handed to.
- It shares the two running slots like any other VM, so stop it (`accounts revoke`) when the work is done.

What this does not cover:

- Every agent on this Mac runs as your user. The session check on guest commands is a lampvm rule that agents follow; a process that ignores lampvm can still `tart exec` into the VM while you are signed in, or copy its disk under `~/.tart/vms` while it is stopped. What the guest itself enforces is that a booted VM gives nobody a command line until you sign in, and that a signed-in session has no root without your password.
- FileVault is off in the guest, so the stopped disk is not encrypted. The login keychain is encrypted with your password, but browser cookies that are not kept in the keychain (Safari's, for one) can be read from a copy of the disk.
- System Integrity Protection stays off, as in every lampvm image, since lampvm grants its helper Accessibility and Screen Recording by writing the guest's permission database. Without a passwordless `sudo`, an agent needs your password before that matters.

## Declare a VM in a spec

Instead of cloning, starting, installing and copying step by step, describe the VM in a JSON file and run `up`. It returns, printing `ready: NAME`, once the VM is running and every ready check has passed:

```sh
export LAMPVM_NAME="lamp-agent-$(uuidgen | tr '[:upper:]' '[:lower:]')"
export LAMPVM_SESSION="<your task or thread id>"
bin/lampvm up specs/xcode27.json --owner "what this run checks"
bin/lampvm exec /usr/bin/xcodebuild -version   # the VM is ready for commands
bin/lampvm destroy --yes                       # only your clone; the image stays
```

```json
{
  "image": {"name": "lampvm-xcode27", "from": "ghcr.io/cirruslabs/macos-tahoe-xcode@sha256:71d9…"},
  "install": ["../build/Lamp Dev.app"],
  "files": [{"from": "fixtures/made-up-project", "to": "/Users/Shared"}],
  "launch": "Lamp Dev",
  "ready": [
    {"exec": ["/usr/bin/xcodebuild", "-version"], "expect": "Xcode 27"},
    {"app": "Lamp Dev", "text": "Ready", "timeout": 60}
  ]
}
```

- `image` is the prepared image the VM is cloned from: what is stable and expensive to copy, such as Xcode and its simulators, lives there instead of being pushed into every VM. `from` is a public Tart image pinned by tag or `@sha256` digest, or a stopped local VM; `cpu`, `memory` and `display` are optional. The first `up` naming an image that does not exist yet prepares it once with `setup` (download, guest helper, automation permissions, a stopped clean copy), without changing the default clean image in runtime `config.json` and without rebuilding the shared helpers. Later specs naming it clone it as it is. Change the image by giving it a new `name`.
- `install` lists host `.app` builds copied into guest `/Applications`, as `install` does; `files` lists host files or folders streamed into guest folders, as `push` does; `launch` opens an app. Host paths are relative to the spec file.
- `ready` says what "ready" means. `{"exec": [...]}` runs a guest command until it exits 0 (and its output contains `expect`, if given); `{"app": ..., "text": ...}` waits for that text, whatever the app shows once it is ready, in its Accessibility tree. Each check has `timeout` seconds (default 60). A check that never passes fails `up` with its last output and leaves the VM running for inspection.
- `up` needs a private name (`--name` or `LAMPVM_NAME`), waits for a slot as `start --wait` does (`--wait`, default 3600 seconds) with `--session`, `--owner` and `--idle-after`, and a rerun of the same line resumes: it reuses the VM if it was made from the same image, then installs, copies and checks again. Hold, stop and destroy the VM as any other.

`specs/xcode27.json` uses Cirrus Labs' Tart image with Xcode 27 and its simulators, which they build with Packer from their [macOS image templates](https://github.com/cirruslabs/macos-image-templates). It is a 62 GB download; the prepared image is a 140 GB guest disk taking about 68 GB of the host's. Clones share its blocks until they change them. Once the image exists, `up` clones it locally, so the downloaded copy in Tart's cache (`tart list` shows it as `OCI`) can be deleted; it shares the image's blocks, so that frees little. Preparing it waits for a free slot without joining the `start` line, as `setup --wait-slot` does.

## Share the two running slots

Use the same runtime state directory for sessions sharing the queue. `start --wait`
creates a persistent FIFO ticket and launches a detached worker with no stdin or
terminal dependency. Killing the calling shell/waiter does not cancel the ticket:
the worker still starts that private VM when its turn arrives, until the original
waiting deadline expires. Boot readiness is checked separately after admission.
A retry for that name keeps its original place, owner, session and deadline; extending
`--wait` on a retry does not jump or prolong the queue.

`start --wait SECONDS` is the one way to wait: it returns only when the VM has a
slot, has booted and is ready for guest commands, printing `running: NAME` and
exiting 0. Run the next guest command straight after it. Do not poll `status`
for this: a booted VM that nobody is using still occupies one of the two slots
while others queue. If your shell or agent tool cuts the command off first, the
ticket and its place are kept: run the same line again and it resumes waiting,
or returns at once if the VM became ready meanwhile. A nonzero exit names the
reason (failed, expired, cancelled). `--detach` returns as soon as the ticket is
saved, for a session that has other work to do while queued; when that work is
done, run `start --wait SECONDS` without `--detach` to wait for the same ticket.

Give every start of one session the same `--session ID` (or export
`LAMPVM_SESSION`): the task or thread id your session already has, one token
without spaces. `status` shows it on each holder and queued ticket and lists
them per session, so a session holding one slot while queueing for another is
visible. `--owner` remains a free description. Tickets saved without a session,
and VMs started outside lampvm, keep working and are listed under `no session id`.

```sh
export LAMPVM_NAME=test-my-session
export LAMPVM_SESSION="<your task or thread id>"   # one token such as t_abc123, the same on every start
L=./bin/lampvm
$L start --wait 3600 --owner "scenario session"   # returns when ready for commands
$L status                 # all holders, start times, idle time, queue order/age, sessions
$L status --json          # timestamps, last command, active work, logs, deadlines
# Declare a long scenario that runs inside the guest:
$L hold --seconds 3600 --reason "40-minute scenario driven inside the guest"
$L exec /path/to/scenario # foreground exec/sh are tracked while they run
$L release-hold
$L stop                   # only your private VM; also cancels its queued start
$L destroy --yes
```

The worker flags a holder after 10 minutes without a guest command (adjust with
`start --idle-after SECONDS`). It records attention transitions in the ticket's
log and shows the flag in `status`. It **never stops a VM for silence**: a quiet
guest could be doing legitimate work. A live lampvm command, including a long
`exec`, `sh`, wait, transfer or recording, suppresses the idle flag. A timed hold
suppresses it for autonomous guest jobs even if their host driving process dies;
after the hold expires, the worker flags command silence again. A flag means
“inspect your own session”, not “the guest is safe to kill”. Status polling does
not refresh usage. Stop/destroy your own clone when finished.

`cancel-start` removes only the selected owned VM's queued request, without
stopping a VM. Admission in progress completes under its lifecycle lock before
cancellation can run. Expired/cancelled tickets no longer block the line. Immediate
`start` cannot jump ahead of waiters. The status includes unowned/older running
VMs as holders with unknown owner/start/activity, and cannot infer an ETA for
guests with arbitrary jobs.

If a detached worker itself is killed, status marks it stalled. Rerun `start`
with the same name to recover the saved ticket (or the monitor for an already
running managed VM). Its waiting deadline still counts elapsed wall time. Host
restart does not restart workers; retry unexpired tickets explicitly. Workers
exit when their VM stops. There is no background shutdown/reaping service.

Between starting and finishing, three rules keep a session from queueing twice:

- Keep the VM running under its hold through a rebuild or any pause of a few minutes, and reinstall the new build into it.
- `stop` or `destroy` only when you are done or away for long: they give the slot up, and `start` after `stop` joins the back of the line.
- `reset --yes` only when you need a clean guest: it keeps the slot and leaves the VM running, so `stop` or `destroy` afterwards.

`reset --yes` on a running VM is for a clean guest in the middle of one session's
work: it stops the VM, replaces it from its clean source and boots it again
without giving up the slot, returning when the new guest is ready for commands.
Nobody is admitted in between, the ticket and its start time stay the same, and
`status` counts the resets. It cannot hold a slot open: each step is bounded, and
a reset that fails closes the ticket unless the VM was left running for
inspection. A stopped VM is reset as before and stays stopped; one that is still
queued keeps its place in line. `stop` and `destroy` give the slot up at once, and
a `start` after `stop` joins the back of the line.

`stop` and `destroy` close the VM's ticket themselves, as does a `reset` that leaves
its VM stopped, so a ticket never depends on its worker surviving. `status` also settles tickets against Tart: a
`running` or `failed` ticket whose worker is gone (its worker lock is free) and
whose VM Tart no longer runs is closed, and a waiter whose worker is gone is
expired once its deadline passes. This never stops a VM. Never kill lampvm
processes by name (`pkill -f lampvm...` also kills the worker and the VM); use `stop`.

Finished tickets (stopped, cancelled, expired, or failed with the VM gone) stay
in `status --json` for one hour, then `status` forgets them. Slot/boot logs and
lock files older than an hour are deleted only for names that have no ticket, no
holder, no ownership record and no Tart VM; a lock is removed only while held.
The FIFO applies to this runtime state's updated `start` commands; old binaries,
other state directories, direct Tart and provisioning `setup --wait-slot` do not
participate. All local running VMs still count toward the two-slot capacity.

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

`record` blocks until `record --stop` ends it or its `--seconds` run out, then copies the movie to the host and reports how long the take ran. Start it in the background, drive the app, then stop it. The guest helper records the display itself with ScreenCaptureKit and closes the movie when stopped, so the movie ends at the moment of stopping. It needs Screen Recording for the helper, which `setup` already grants along with Accessibility by writing the guest's permission database (possible because the test image has SIP disabled); nothing new is granted.

### Recording a video for a pull request

A viewer sees every second the screen stands still, so the whole take is one script, written and tried before recording:

1. Rehearse first without recording: find each label with `tree`/`find`, run every step once and settle the script. Reset the app to its starting state afterwards.
2. Run the script below in one shell call. Do not issue the steps as separate tool calls: the time you spend reading output between them is a frozen screen in the video.
3. Wait on what the app shows (`wait APP TEXT`, `wait ... --absent`), never `sleep`. `wait` returns the moment the text is there. Keep a `sleep` of a second or less only where the viewer needs to read a result.
4. End with `record --stop` as soon as the last result is on screen. `--seconds` is only the upper limit (3600 at most), so a take is never sized in advance and nothing has to be cut out afterwards.

```sh
$L record /tmp/flow.mov --seconds 600 &      # upper limit; stopped below
$L wait Test 'Continue' --exact --timeout 10  # also gives the recording time to start
$L clickon Test 'Continue' --exact
$L clickon Test 'Name' --exact
$L type 'made-up input'
$L key return
$L wait Test 'Ready' --exact --timeout 60     # however long the app takes, and no longer
sleep 1                                       # let the viewer read the result
$L record --stop
wait                                          # the record call saves /tmp/flow.mov
```

`record --mark 'Request sent'` names the moment for the running recordings: the guest helper notes it on the clock its frames are timed by, so a mark lands where it was made even after a pause. The marks come back in the `record` call's report and in `take.timeline.json` beside the movie, and `video` draws them as captions. Mark what the viewer is about to see, in plain words, at the moment it happens: before sending a request, when the app shows a result, when a turn ends.

### Making the take worth watching

A real turn has long stretches where nothing moves while a model thinks or a build runs, and a viewer sits through every one of them. `video` cuts a finished recording on the host, with no VM and no ffmpeg:

```sh
$L video /tmp/flow.mov /tmp/flow.mp4 --condense 3 --crop 0,60,2880,1600 --width 1920
```

- `--condense SECONDS` plays every stretch that stands still for at least that long fast, so it lasts about a second and a half, with a badge in the corner saying how long it really was ("▶▶ 48 s pass"). The first second of each stretch (`--hold`) stays at real speed, so the result the screen reached can be read. A frame counts as still while fewer than 0.3% of its pixels have moved since the stretch began, so a clock tick, a breathing line or the pointer do not break a stretch, while typing or streaming text soon does.
- The marks from `record --mark` (the recording's own `take.timeline.json`, or `--timeline FILE`) are drawn in a black band added under the picture, so they cover nothing, each from its moment to the next mark's. A long caption wraps to fit the movie's width; one that needs more than three lines is cut at a word with "…", and the report gives what was shown as `shown`. A movie without marks has no band.
- `--crop X,Y,W,H` keeps one part of the picture, in the movie's pixels: twice the points `bounds` reports on a Retina guest. `--width` scales the result and never upscales.
- The report gives the finished movie's length and size (`height` includes the caption band, `captionBand` its height), each condensed stretch (where it was, how long it was, how long it plays) and each caption's time in the finished movie, so a message can list what shows when.

Take the whole desktop or the windows that matter at their real size and crop with `video`, rather than scaling a whole desktop down to a small movie where nothing can be read.

When part of the flow must not be filmed, such as a person signing in, run `record --pause` before it and `record --resume` after it. Nothing on screen in between is kept, and the movie carries on from the moment of pausing without a gap, showing the screen as it is at the moment of resuming. The `record` call reports the time left out as `pausedSeconds`. A pause that is never resumed ends at `record --stop`. `--seconds` counts paused time too, so give a take with a long pause a higher limit. To see the screen while paused, a person can open the VM with `view`.

`record --text-check 'name@example.com|Company'` reads the finished movie on the host and fails if any of those words show, giving the seconds where they do; the movie is kept so it can be covered or deleted. It reads every frame, so a single frame's flash is caught too; a mostly still take has few frames, while a long take full of motion takes longer to check. It first draws each word alone into a plain picture and must read it back, so a word text recognition cannot read is refused rather than reported clean. `status` lists each running recording, how long it has run and whether it is paused.

```sh
$L record --pause                             # sign-in starts; nothing is filmed from here
$L wait Test 'Signed in' --exact --timeout 600
$L record --resume                            # filming carries on from the signed-in screen
```

While a recording of the VM is running, `move`, `click`, `doubleclick`, `rightclick`, `drag`, `scroll` and `clickon` carry the pointer to the target in 0.12–0.35 seconds instead of jumping there; `--glide` on any of them does the same without a recording. A VM keeps the guest helper it was prepared with. One whose helper predates the glide (`vmctl version` fails or reports less than 2) is driven without it during a recording, and `--glide` there is an error. One whose helper predates the recorder (less than 3) records with the guest's `screencapture` as before: the take runs for its full `--seconds` and `record --stop` is an error. One whose helper is older than 4 records for 300 seconds at most, and `record --pause` and `--resume` are errors there. One older than 5 cannot bring a single window forward for `--window`: its app comes forward, so another of the app's windows may cover it, and the report says so; it also checks for covering windows at five points rather than across the whole window, and it films notification banners. One older than 6 cannot take `record --mark`. Clones of a clean image prepared by the current `setup` have all of these. Prefer `clickon` with a label to `move` then `click` with coordinates: it is one step on screen instead of two. If the app itself makes the viewer wait (a build, a model reply), say so in the pull request rather than cutting the video.

Cropping uses the app window, not guessed screenshot coordinates. `--window TITLE` on `shot` and `record` crops to one window instead: the window with that exact title, or else the only one whose title contains it, in any app unless `--app` names one. Several matches, or none, is an error listing the windows. The window is brought forward first; one that stays covered by another app, such as a desktop widget, is an error rather than a picture of what covers it. A notification banner over the captured area is an error for every `shot`, whole desktop and app window included, since its text is often private; wait for it to go or dismiss it. Recordings by a helper of version 5 or later leave Notification Center out of the picture, so a banner is never filmed, even one arriving mid-take, and nor are desktop widgets; an older helper's recording is refused at the start if a banner covers its area, but not if one arrives later, so use `--text-check` there. Screenshots use a unique guest temporary filename on every call; omitting the host destination also gives a unique output name. Concurrent captures do not overwrite each other's temporary files, but concurrent UI interactions still require separate VMs.

Coordinates use screen points with the origin at the top left. They are not screenshot pixels: Retina screenshots may have twice as many pixels. Read `tree`, `find` or `bounds` before clicking. Prefer exact labels and verify observable state afterwards. A successful click hit report confirms the target was under the pointer; it cannot prove the app completed its action.

## Commands

Global selection: `--name NAME`, `--config FILE`, `--state-dir DIRECTORY`. Environment equivalents: `LAMPVM_NAME`, `LAMPVM_CONFIG`, `LAMPVM_STATE_DIR`; `LAMPVM_SESSION` is the task or thread id for `start`; `LAMPVM_APP` sets a default target app; an `app` in the file given with `--config`, including `"app": null` for the whole desktop, takes its place for that command, as does `--app`; `LAMPVM_TART` selects an existing Tart executable. See `help` for exact argument order.

| Command | What it does |
|---|---|
| `setup` | Downloads dependencies, builds tools and prepares the guest and clean image. `--from VM` uses a stopped local source. |
| `build` | Builds the host lock-state, text-check and video-editing helpers and the guest input/inspection helper into external state. |
| `video SOURCE FILE [--condense SECONDS] [--hold SECONDS] [--crop X,Y,W,H] [--width N] [--timeline FILE]` | Cuts a finished recording into a movie worth watching, on the host without a VM: still stretches played fast with a badge, marks drawn as captions, a crop and a width. The report lists the condensed stretches and where each caption falls. |
| `doctor` | Checks host dependencies, Accessibility and reported guest health, including unfinished Setup Assistant. It does not prove video consent or every interaction. |
| `start [--wait SECONDS] [--session ID] [--detach]` | Durable FIFO admission, detached boot worker and idle observation. Returns when the VM is ready for commands; rerun to resume the same ticket. `--session` (or `LAMPVM_SESSION`) is the task or thread id `status` groups by; `--owner` is a free description. `--detach` returns once the ticket is saved. |
| `up SPEC [--wait SECONDS] [--session ID]` | Makes the VM a spec file describes from its prepared image (preparing the image first if it does not exist), installs and copies what the spec lists and returns once its ready checks pass. |
| `stop` | Shuts down the selected VM, then forces it off if graceful shutdown times out. |
| `status [--json]` | Shows every local slot holder, activity/holds, worker health and FIFO line, grouped per session (`sessions` in JSON), and each holder's running recordings; includes host lock state if built. |
| `view [NAME]` | Opens a running VM in a Screen Sharing window on the host for a person to watch or use. Defaults to the selected VM, else the only one running. Takes no lock and records no activity. For an accounts VM it gives no password; you type yours. |
| `cancel-start` | Cancels only the selected owned VM’s queued start. |
| `hold --seconds N --reason TEXT` / `release-hold` | Declares/clears autonomous guest work; protects it from idle flags. |
| `apps` | Lists running guest apps with process names, bundle IDs, PIDs and frontmost status. |
| `ocr [--app APP]` | Reads visible text with offline Apple Vision OCR, including confidence and text boxes in image pixels. App captures include `screenOriginPoints` and `pixelsPerPoint` conversion; boxes have a top-left image origin. |
| `accounts create NAME` / `grant NAME --session ID` / `revoke NAME` / `destroy NAME --yes` / `list` | A persistent VM for real sign-ins that only its owner opens and hands to one session at a time; see [accounts VMs](#sign-in-once-and-hand-the-vm-to-one-agent). `create` asks for a new guest password; `grant` and `destroy` ask for it again. |
| `clone NAME [--from VM]` | Creates a separate working VM from a stopped clean source. Never from an accounts VM. |
| `destroy --yes` | Stops and deletes the selected owned working VM; clean images are protected. |
| `reset --yes` | Replaces the selected working VM with a fresh clone of the configured clean image. A running VM reboots in the slot it holds and is ready when the command returns; a stopped one stays stopped. |
| `install PATH` | Copies an app to guest `/Applications`, quits the previous copy and clears copied extended attributes. |
| `launch [APP] [ARGS…]` | Opens an app, passing optional launch arguments. |
| `quit [APP]` | Requests that an app quit. |
| `activate [APP]` | Brings the app forward. |
| `openrouter [APP] [--model ID]` | Lamp only: quits the app, copies just the `openrouter` entry of the host's `Lamp Dev/keys.json` into the guest app's `keys.json` (mode 600, sent on standard input) and sets the app to build with pi on OpenRouter using the configured test model, `anthropic/claude-sonnet-5.5`. APP is `Lamp Dev` unless given. |
| `wipe --yes` | Quits the configured app and clears only explicitly configured preference domains/data paths. It does not erase the guest or Keychain. |
| `tree [APP] [--json]` | Reads the app's Accessibility tree: roles, frames, labels and values. |
| `find [APP] TEXT [--json] [--exact] [--role ROLE]` | Finds labelled Accessibility elements; role restricts matches (for example `AXButton`). |
| `windows [APP]` / `bounds [APP]` | Lists app windows / reports an app window's screen rectangle. |
| `clickon [APP] TEXT` | Activates the app, finds an element and posts a real click, then reports the target/hit result as JSON. Use `--exact`, `--role ROLE`, zero-based `--index N`, or `--right`. `--ocr` finds TEXT by reading the screen instead, for buttons Accessibility cannot see such as a web page's: it reads the app's window, the window named by `--window`, or with no app the whole screen. It matches whole words, and text recognition is approximate: it aims at the words' share of a line, so verify the result. |
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
| `shot [FILE] [--app APP] [--window TITLE]` | Captures the desktop, or activates/crops to the selected/configured app window or the named window, and copies the PNG to the host. |
| `record [FILE] [--seconds N] [--app APP] [--window TITLE] [--text-check WORDS]` | Records the guest display, or the selected/configured app's or named window's area, for up to N seconds (1–3600, default 5) and copies the movie to the host. `record --stop` ends the VM's running recordings now; each waiting `record` call then saves its movie. `record --pause` and `record --resume` leave the time between them out of the running recordings. `record --mark TEXT` names the moment; the marks come back with the movie and in `take.timeline.json` beside it. `--text-check` fails the call if the movie shows any of the `|`-separated words. |
| `exec COMMAND [ARGS…]` | Runs an executable as the guest's logged-in user. If a password or Keychain prompt opens meanwhile, it stops waiting within a few seconds (a prompt already open when it starts is left alone), prints the prompt's text and buttons and fails; the prompt stays open for a person to answer through `view`, or to refuse with `press SecurityAgent Cancel` (or `Deny`). |
| `sh` | Runs stdin as a guest zsh script, watched for prompts as `exec` is. |
| `push SOURCE DEST` | Streams a host file/folder into a guest directory. |
| `pull SOURCE DEST` | Copies one guest file to the host. |
| `help` | Lists commands and options. |

`key` supports letters, digits, common US-layout punctuation, Return/Enter, Tab, Space, Backspace/Delete, ForwardDelete, Escape, arrows, Home/End, PageUp/PageDown and F1–F12. `type` posts Unicode rather than mapping characters through a physical keyboard layout; tabs and newlines send Tab and Return. Coordinate input commands, `type` and `key` also accept `--app APP` to activate the app first.

The guest helper has two low-level diagnostics in addition to the wrapper commands: `vmctl trusted` reports Accessibility trust, `vmctl mark TEXT` is what `record --mark` runs, and `vmctl display [WIDTH HEIGHT]` lists display modes or changes the display. The installed path is `$HOME/.local/bin/vmctl` in the guest; inspect/use it through `exec` or `sh`, rather than assuming a guest username.

## Configuration, including Lamp

The default is app-agnostic. JSON configuration supplies `vm`, `clean`, `image`, `cpu`, `memory`, `display`, optional `app`, and optional `wipe.preference_domains` / `wipe.paths`. `--config FILE` or `LAMPVM_CONFIG` merges a template over runtime state `config.json`. Templates are read-only: setup records effective choices in runtime state, without rewriting the template. Use unique names/state directories for independent setups. Paths used by `wipe` are relative to the guest home and limited to app-data directories under `Library/Application Support/` or `Applications/`. Keep your local configs outside git if they contain private details. Review any wipe configuration before running it.

An opt-in Lamp example is provided in `config/lamp.json`. Its bundle ID, preference domains and application data paths preserve Lamp's existing Forge identifiers. Select it explicitly:

```sh
bin/lampvm --config config/lamp.json --name test-my-session launch
```

### Live AI turns in Lamp

Real turns are paid from the host owner's OpenRouter key, so VM tests build with Sonnet 5.5, not Lamp's own default (Opus 5.5). After installing a source build, run `openrouter` instead of writing the key or model settings by hand; it also completes onboarding:

```sh
bin/lampvm --config config/lamp.json install "build/Lamp Dev.app"
bin/lampvm --config config/lamp.json openrouter
bin/lampvm --config config/lamp.json launch "Lamp Dev"
```

The model comes from `openrouter.model` in `config/lamp.json`; pass `--model ID` only when a test needs another. Lamp has no thinking setting of its own: pi starts at its default level, `medium`, which each turn's pi session file (`Apps/APP/.lamp/pi-sessions/*.jsonl` in the guest's `Lamp Dev` folder) records beside the model. Check those lines when a run's cost matters.

## Limits and security

- The toolkit uses Tart's local control socket/guest agent to execute commands, CoreGraphics events for mouse/keyboard input, Accessibility for inspection, and guest screen capture for images/video. It does not need host screen access or SSH keys.
- The guest has normal network access. No shared folders or clipboard are enabled, but `exec`, `sh`, `push` and apps can deliberately transfer data. Isolation is not a credentials policy: agents must avoid real sign-ins, keys, tokens, personal data and production accounts in test VMs. Real sign-ins belong only in an accounts VM its owner signed in to and handed over.
- Accessibility is only as complete as the app makes it. Canvas/custom controls may expose no labels. Duplicate labels need explicit selection. Popovers, menus, multiple windows and overlays can affect hits. Use screenshots and state assertions; a click report alone is not action verification.
- OCR can help with unlabelled controls, but recognition is approximate. Its boxes refer to the captured image, not screen points; apply the reported crop origin and scale before clicking (`screenX = originX + imageX / scaleX`, likewise for Y). Recordings keep the crop fixed at their start, so keep the app window in place.
- Typing requires focus. Direct value-setting can be unsupported or behave differently from a user's keystrokes. Presence in the Accessibility tree does not always mean an element is visibly on screen.
- The guest's macOS version determines what you can test. A newer host does not make guest-only behaviour match it. Setup defaults to a pinned image digest; if you choose a mutable `latest` tag, it can change. Record `sw_vers` with proof and configure a stable image reference for repeatable runs.
- Audio/microphone and sign-in flows are outside the default setup. Screen capture consent can recur. Host restart does not automatically restart the VM. SIP-disabled test guests should not hold valuable data.
- Setup preserves a VM's hardware identity on retries and refuses to create a clean snapshot after new kernel panic reports or a visible restart-error alert. Start/doctor also check for reported guest health problems. OS/virtualization errors need inspection or a different base image; the toolkit does not dismiss those alerts automatically.
- macOS/Tart licensing applies separately from this toolkit. Source is [MIT licensed](LICENSE), copyright 2026 Roo Code, Inc.; Tart is downloaded separately under its own Fair Source licence. See [licence notices](licenses/NOTICE.md). Do not distribute macOS VM disks or private prepared images with the repository.
- Ownership records are local safeguards. Do not delete and recreate toolkit VMs directly with Tart under the same names, or share a state directory with untrusted processes.

## Verify toolkit changes

`tests/build-harness.sh` builds an offline, ad hoc signed AppKit app under ignored `build/`. It provides a click counter, text field, delayed/hidden element, 60 scrolling rows, a drag source/drop target, a right-click context menu, and a second window, "Harness Notes", whose Approve button is drawn without Accessibility, as a web page's can be, and whose Post Banner button posts a real notification with made-up text (macOS asks once for permission). Install it into a disposable private clone, test the commands through the actual guest, and capture cropped proof. No personal app or account is needed. The visible scroll-offset label verifies that wheel input moved the viewport even when Accessibility exposes offscreen rows.

Run host-side tests with `python3 -m unittest discover -s tests -v`. The slot suite launches real CLI/worker processes against an isolated fake Tart, kills waiters/workers, checks recovery/FIFO/expiry/cancellation and verifies idle flags without stopping guest jobs. These check wrapper behaviour without substituting for VM tests. `tests/smoke.py` drives the harness through a running private VM and saves command output plus cropped screenshots/recording; read its help before running it. Start only your own prepared VM, set its name/state explicitly and delete that clone after proof.

Keep proof, compiled apps, logs and recordings outside the repository. Read the migration notes in [docs/MIGRATION.md](docs/MIGRATION.md) before replacing an existing installation that other sessions use.

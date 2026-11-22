# spicetify-fixer

One click to get **Spotify + Spicetify + the Marketplace** working, and then open Spotify.
No "press y to continue", no re-running commands by hand.

It's a single Python file (`spicetify_fixer.py`) that uses only the standard library, so there's nothing to `pip install`.

## What it fixes, and why it kept breaking

If Spicetify "breaks every time you open Spotify", the usual cause is the **Microsoft Store version of Spotify**:

1. The Microsoft Store updates Spotify by itself and puts each new version in a new folder inside `C:\Program Files\WindowsApps`.
2. Every update moves Spotify away from the folder Spicetify patched, so the patch is gone and you have to re-run Spicetify.
3. Spicetify only partly supports the Store build. Its own guide says to use the desktop installer. The Store build only loads the patch when it is started in a special way (`--app-directory`), so the normal Start Menu tile opens plain Spotify. On the Store build, Spicetify also stops and asks *"Continue backing up anyway? [y/N]"*.

What the tool does about it:

- **Store Spotify (the default):** it **keeps it, with your login**, and just patches it. The Store will still replace Spotify when it updates, so open Spotify **through the tool or its shortcut**: it notices, re-patches only when needed, and starts Spotify the special way that loads the patch.
- **Want it to stop breaking for good?** `--switch-to-desktop` moves you once to the **desktop Spotify** (`%APPDATA%\Spotify`), which Spicetify fully supports. That needs one new login.

Every run does this:

| Step | What happens |
|---|---|
| 1. Find Spotify | Looks for the Store build (Appx package) and the desktop build (`%APPDATA%\Spotify\Spotify.exe`), with their versions. |
| 2. Store check | On the Store build it **keeps Spotify and your login** and goes on to patch it in place. Only with `--switch-to-desktop` does it remove the Store build and install the desktop one (one new login). It never asks a question. |
| 3. Spotify up to date | Compares your desktop Spotify with what winget offers and updates it with winget if it's older. If winget fails, it opens the download page. |
| 4. Spicetify | Installs Spicetify with the official installer if it's missing (it answers the installer's questions, and says yes to the Marketplace). Then it upgrades Spicetify when GitHub has a newer version. |
| 5. Config paths | If `spotify_path` / `prefs_path` in `config-xpui.ini` point at the Store package or at a folder that no longer exists, it sets them to the right Spotify with `spicetify config`. |
| 6. Patch | Works out what Spicetify needs: `apply`, `backup apply` (after a Spotify update), or `restore backup apply` (after a Spicetify upgrade). It answers every prompt by itself and retries once if the first try fails. |
| 7. Marketplace | Checks that `custom_apps` contains `marketplace` and that `%APPDATA%\spicetify\CustomApps\marketplace` exists, and **updates it** when GitHub has a newer release (it reads the installed version from the Marketplace's own files, e.g. v1.0.8 → v1.0.12). Missing or older: it runs the official Marketplace installer (which always downloads the newest release), then applies. |
| 8. Open Spotify | Starts the patched Spotify and closes itself. |

If something goes wrong, the window stays open with a clear message and the path of the log file. When it runs from the shortcut (no window), you get a message box instead.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer (the `py` launcher). Nothing to install with pip.
- `winget` (App Installer), which comes with Windows 10/11. It's needed to install and update the desktop Spotify.
- Internet, for the winget and Spicetify downloads.
- Run it as a **normal user**, not "as administrator". See [Admin vs normal window](#admin-vs-normal-window).

## How to run

Open a normal terminal (not as administrator) in this folder:

```
py spicetify_fixer.py --check     # dry run: shows what it would do, changes nothing
py spicetify_fixer.py             # fix everything, then open Spotify
```

It never asks anything and never needs a "y": every run is fully automatic.

## Commands

| Command | What it does |
|---|---|
| `py spicetify_fixer.py` | Fixes whatever needs fixing (Spotify update, Spicetify update, config paths, patch, Marketplace install and update) and opens Spotify. |
| `py spicetify_fixer.py --check` | Dry run: prints the plan, changes nothing. Good first thing to try. |
| `py spicetify_fixer.py --make-shortcut` | Creates the **"Spotify (Spicetify fixed)"** shortcut on the Desktop and in the Start Menu, then exits. |
| `py spicetify_fixer.py --switch-to-desktop` | Moves from the Store Spotify to the desktop Spotify once (removes the Store app, installs with winget; one new login). |
| `py spicetify_fixer.py --block-spotify-updates` | Same as a normal run, and also stops Spotify updating itself (desktop build only; see the trade-off below). |
| `py spicetify_fixer.py --no-launch` | Fixes everything but doesn't open Spotify. |
| `py spicetify_fixer.py --skip-spotify-update --skip-spicetify-update --skip-marketplace-update` | Only checks the paths and the patch (no updates). |
| `py spicetify_fixer.py --verbose` | Shows every command's full output in the console. |
| `py spicetify_fixer.py --check --no-color` | The dry run in plain text (for copying into an issue). |
| `py -m unittest -v` | Runs the 55 tests (every command is faked; nothing on your PC is touched). |

Flags can be combined, e.g. `py spicetify_fixer.py --skip-spotify-update --no-launch --verbose`.

## Controls: what the colours mean

In a terminal the output is coloured by meaning (turn it off with `--no-color` or the `NO_COLOR` environment variable):

| Colour | Meaning | Example |
|---|---|---|
| **Cyan** | A step starts | `== Step 5: Spicetify config paths` |
| **Green** | Already fine, nothing to do | `Up to date (newest is 2.46.7).` |
| **Yellow** | What it will do, or something that needs fixing | `Needs fixing: spotify_path points at the Store package` |
| **Red** | A problem | `FAILED: …`, `timed out after …` |
| (none) | Plain information | `spotify_path = …` |

The shortcut runs without a window; if something fails there, a message box says what and where the log is.

### The shortcut (recommended)

```
py spicetify_fixer.py --make-shortcut
```

This creates **"Spotify (Spicetify fixed)"** on your Desktop and in the Start Menu, with Spotify's icon. The shortcut runs the tool silently with `pythonw`: it fixes whatever needs fixing and opens Spotify. Use it instead of the normal Spotify tile or shortcut. You can pin it to the taskbar or Start.

If you move the project folder, run `--make-shortcut` again.

## Flags

| Flag | What it does |
|---|---|
| `--check` | Dry run. Reports what it would do and changes nothing. Only read-only commands run (`Get-AppxPackage`, `winget show`, `spicetify -v`, `spicetify -c`, `tasklist`). |
| `--make-shortcut` | Creates the Desktop and Start Menu shortcuts, then exits. |
| `--switch-to-desktop` | Moves from the Store build to the desktop build once: removes the Store app (its login and downloads go), installs the desktop one with winget. If a switch is cut off half-way, the next run finishes it. |
| `--never-switch` | Accepted for older shortcuts: the Store build is kept by default anyway. |
| `--skip-spotify-update` | Doesn't update Spotify. |
| `--skip-spicetify-update` | Doesn't upgrade Spicetify. |
| `--skip-marketplace-update` | Doesn't update the Marketplace (it is still installed if missing). |
| `--block-spotify-updates` | Runs `spicetify spotify-updates block` (desktop build only). See below. Off by default. |
| `--no-launch` | Doesn't open Spotify at the end. |
| `--no-pause` | On failure, doesn't keep the window open (and shows no message box). |
| `--no-color` | Plain text in the console. Normally steps are cyan, what is already fine is green, what it will do or must fix is yellow, problems are red. |
| `--verbose` | Shows every command's output in the console. The log file always has it. |

### `--block-spotify-updates`: the trade-off

The desktop Spotify updates itself in the background. Each update replaces its files, so the patch is gone until the next run of this tool. If you always open Spotify through the shortcut, that's harmless: the next start re-patches it automatically.

With `--block-spotify-updates`, Spotify can't update itself. It only changes when this tool updates it through winget, so the patch survives.

- **Pro:** the patch never disappears between runs, and updates only happen while the tool is in control.
- **Con:** you get new Spotify versions only when winget has them, which is sometimes a few days later. If you ever run the tool with `--skip-spotify-update`, you stay on the old version. To undo it, run `spicetify spotify-updates unblock`.

## Admin vs normal window

- **Use a normal window**, or the shortcut, which runs as you.
- Spotify's installer **refuses to run as administrator**. When the tool is elevated, it won't switch, install or update Spotify, and it tells you why *before* removing anything.
- Spicetify also refuses admin, because files patched by an administrator can give a black Spotify window. If you do run elevated, the tool adds `--bypass-admin` and warns you that it isn't recommended.

## What it changes on your PC

Nothing runs in `--check` mode except the read-only commands listed above. It only writes its own log file. A real run can:

- **Remove the Microsoft Store Spotify**, only with `--switch-to-desktop` (for your user only: `Get-AppxPackage SpotifyAB.SpotifyMusic | Remove-AppxPackage`). This deletes that app's downloads and saved login, so **you'll need to log in again**. Without that flag the Store Spotify and your login are kept.
- **Install or update the desktop Spotify** with `winget install` / `winget upgrade --id Spotify.Spotify -e --silent`. It goes to `%APPDATA%\Spotify`.
- **Start Spotify once by itself** right after installing or updating it. Spotify writes its settings file (`prefs`) on its first start, and Spicetify needs that file. The tool waits for it (up to 2 minutes), closes Spotify, patches it, and opens it again. If you were in the middle of logging in, just log in again after it reopens.
- **Close Spotify** before patching or updating it.
- **Install or upgrade Spicetify** with its official installer / `spicetify upgrade`. It goes to `%LOCALAPPDATA%\spicetify`, and the installer adds it to your user PATH.
- **Change two lines in Spicetify's config** (`%APPDATA%\spicetify\config-xpui.ini`): `spotify_path` and `prefs_path`, only through `spicetify config`.
- **Patch Spotify** with `spicetify backup / restore / apply` (always with `-n`, so Spicetify doesn't restart Spotify itself).
- **Install the Marketplace** with its official installer if it's missing.
- **Block Spotify's own updates**, only with `--block-spotify-updates`.
- **Create shortcuts**, only with `--make-shortcut`. After a switch to the desktop build, existing shortcuts are re-saved so their icon points at the new Spotify.
- Write its own files in `%LOCALAPPDATA%\spicetify-fixer\`: `spicetify-fixer.log` (with rotation, max ~2 MB in total) and `state.json` (whether a switch was started or done, and which Spotify build it last synced).

It never deletes Spicetify's themes, extensions or custom apps, and it never touches your Spotify account.

## Troubleshooting

- **Where's the log?** `%LOCALAPPDATA%\spicetify-fixer\spicetify-fixer.log`. Every command and its full output is in there.
- **"Spotify's installer refuses to run as administrator"**: close the window and run the tool from a normal one, or from the shortcut.
- **winget failed / isn't installed**: the tool opens <https://www.spotify.com/download/windows/>. Download `SpotifySetup.exe`, run it normally (not as admin), then run the tool again.
- **"Spicetify could not patch Spotify"**: run `py spicetify_fixer.py --verbose` to see Spicetify's own message. If Spotify's files are patched but no backup exists, the tool reinstalls the desktop Spotify to get clean files.
- **Black or blank Spotify window**: usually caused by running Spicetify as administrator. Run the tool once from a normal window.
- **I said "no" to the switch and changed my mind**: run `py spicetify_fixer.py --switch-to-desktop`, or delete `%LOCALAPPDATA%\spicetify-fixer\state.json` to be asked again.
- **Marketplace missing in Spotify**: run the tool again. It reinstalls the Marketplace when `custom_apps` or its folder is missing.

## Development

```
py -m unittest -v
```

The tests fake every command and file, so they never touch your Spotify or Spicetify. GitHub Actions runs them on `windows-latest` with Python 3.11 on every push and pull request.

## License

MIT, see [LICENSE](LICENSE).

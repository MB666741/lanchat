English | [简体中文](README.md)

# LanChat — LAN instant messaging and file transfer

LanChat is a peer-to-peer communication tool for local area networks (LAN). Devices on the same
subnet discover each other as soon as they are started, with no central server, no account
registration and no pairing code: they establish an end-to-end encrypted session directly and
exchange text messages and files. Messages and files travel only between the two endpoints, never
through any third-party node.

A portable Windows build is provided (the target machine needs no Python installation), and the
program can also be run directly from source.

![Self-test mode: two real windows open on the same computer](docs/screenshot-selftest.png)

## System requirements

| Item | Requirement |
| --- | --- |
| Operating system | Windows 10 / 11 (64-bit). Development and testing were both done on Windows; POSIX (macOS / Linux) branches exist in the source but have not been fully verified |
| Runtime | The portable build needs no dependencies; running from source requires Python 3.11 or later and `cryptography` |
| Network | All devices are on the same LAN (the same switch or the same Wi-Fi subnet); across subnets, layer-3 connectivity must already be provided by Tailscale / WireGuard or similar |
| Firewall | On first run you must allow access on "Private networks" in the Windows Firewall prompt, otherwise the devices cannot discover each other |

## Download and installation

1. Download `LanChat-2.3.0-windows-amd64.zip` (about 14 MB) from the [Releases](../../releases) page.
   > If downloads are slow in China, you can use [GitHub acceleration](https://gh-proxy.com/).
2. **Fully extract** the archive to any folder (inside it is a single `LanChat\` folder), then double-click `LanChat.exe` in it.
   The program is distributed as a folder: `LanChat.exe` depends on the `_internal\` folder next to it, so do not copy the executable on its own.
3. Enter a nickname and the program is ready to use.

You can also run it directly from source, or build it yourself. Note that the dependencies are
**two separate things**:

| Purpose | What you need | How to install it |
| --- | --- | --- |
| Running from source | Python 3.10 or later, `cryptography` | `pip install -r requirements.txt` |
| Building an exe | All of the above **plus PyInstaller** | Install it **separately**: `pip install pyinstaller` (it is deliberately *not* in `requirements.txt`) |

```bash
pip install -r requirements.txt        # runtime dependency: cryptography only
python chat_gui.py                     # run it directly
```

Packaging is best done inside a dedicated virtual environment; the complete steps (including how to
verify the build) are in [Development and build](#development-and-build) below.

> The program is not code-signed, so Windows SmartScreen may warn about an "unknown publisher"; choose "Run anyway".
> The identity key and configuration are stored in the data folder `%USERPROFILE%\.lanchat`.

## Usage flow

**Step 1: Add a contact.** Click "Add contact" and the list shows the other devices already discovered on the LAN.
If the other side has invisible mode enabled, or the two sides are on a network where broadcast does not
reach (Tailscale / WireGuard / across subnets), enter the other side's `IP` (the program probes its TCP
port automatically) or `IP:port` manually at the bottom of the same window.

**Step 2: Establish the friendship.** Select the device and send a friend request. A "New friends"
notification appears in the other side's window, and after they click "Accept" the two sides become
contacts. A device that has not been accepted cannot send any messages or files.
If the other side has set a verification question, it must be answered correctly first.

**Step 3: Communicate.** Select a contact on the left to chat, and click the "File" button to choose the file to send;
the transfer starts once the receiver confirms.

## Features

| Feature | Description |
| --- | --- |
| Automatic discovery | Devices on the same LAN appear to each other automatically, with no IP to enter and no QR code to scan |
| Add manually | When broadcast does not reach (across subnets, Tailscale / WireGuard and so on), enter the other side's `IP` (the port is probed automatically) or `IP:port`; Chinese colons, full-width digits and extra spaces are corrected automatically |
| Text chat | One-to-one private chat, plus one-click send to all online friends; with unread counters, and the conversation you have open does not accumulate unread messages |
| File transfer | Download folder can be chosen, files with the same name get a numeric suffix instead of being overwritten, whole-file SHA256 verification, large files supported |
| Friend management | Adding a friend requires the other side's consent, verification questions are supported, and contacts can be blocked / unblocked / removed (removal notifies the other side) |
| End-to-end encryption | Each device has one long-term identity key pair; every connection negotiates a session key on the spot and messages use AES-256-GCM; the key rotates automatically every 30 messages and once before and after each file transfer |
| Invisible mode | With "Allow others to discover me" turned off, the app no longer broadcasts or answers probe requests, and only accepts connections started by the other side entering `IP:TCP port` manually |
| Session history | Kept in memory only and cleared when the program exits, never written to disk; while one side is still online, the other side receives the missing messages again when it comes back online |
| Interface language | Three interface languages: Simplified Chinese / Traditional Chinese / English. The first start follows the system language, and you can switch in Settings (effective after a restart) or specify one temporarily with `--lang` |
| Self-test mode | The whole flow can be verified on a single machine: `--self-test` opens two real windows and provides a "Disconnect for 6 seconds" drill button |

## Interface layout

```
┌───────────────────────────────────────────────────────────────────────────────────────┐
│ Me: Nickname [Settings] [Notifications] [Answer question] [Add contact] [New friends] │
├───────────┬───────────────────────────────────────────────────────────────────────────┤
│ Contacts  │ Conversation (click "Send" or Ctrl+Enter)                                 │
│ (unread)  │                                                                           │
│           │                                                                           │
│           │ [Input box]                                                 [File] [Send] │
└───────────┴───────────────────────────────────────────────────────────────────────────┘
```

## Interface language

The interface ships with three languages: **Simplified Chinese / Traditional Chinese / English**.

* The first start follows the Windows system language (an English system shows English directly, a Traditional Chinese system shows Traditional Chinese).
* Afterwards choose one in the "Settings" → "Language" drop-down and click "Apply" to save it; **it takes effect after restarting the program**.
* You can also specify one temporarily without changing the settings: add `--lang` on the command line, for example `LanChat.exe --lang en`.
  The language codes are `zh-Hans`, `zh-Hant` and `en`.

The English interface:

![English interface](docs/screenshot-ui-en.png)

The translation covers the graphical interface, the user-visible messages of the service / connection /
encryption layers, and the `--diagnose` self-check report;
code comments, startup logs and the development documentation remain in Chinese.

## Security design and protections

LanChat uses no central server and does not rely on a certificate authority (CA); the trust relationship is established through "device identity key + manual fingerprint verification".
The concrete implementations are listed below by protection goal.

### 1. Identity authentication and trust establishment

| Protection goal | Implementation |
| --- | --- |
| Device identity cannot be forged | Each device generates an Ed25519 long-term identity key on first start. The device identifier `peer_id` is derived from the SHA-256 digest of the Ed25519 public key (`lc-` plus 32 hexadecimal digits) and cannot be forged out of thin air |
| Manual identity verification | The interface provides a readable fingerprint of the form `AB3F-91C2-77DE-0A55` (that is, the first 16 hexadecimal digits of `peer_id`, 64 bits in total). The two sides can compare it face to face, over the phone or through another out-of-band channel to detect a man in the middle |
| Handshake identity proof | During the handshake, the transcript digest is signed with Ed25519 and verified, proving that the peer really holds the corresponding identity private key |
| Friend requests cannot be forged | A friend request carries the sender's signature and a timestamp, and the receiver handles it only after verification passes |

### 2. Session encryption and key management

| Protection goal | Implementation |
| --- | --- |
| Key agreement | Every connection generates an X25519 ephemeral key pair on the spot and performs ECDH. The session key is derived with HKDF-SHA256, the salt binds the session ID and the handshake digest, and the two directions of sending and receiving use mutually independent keys |
| Message encryption | AES-256-GCM. The additional authenticated data (AAD) binds the session ID and both `peer_id` values (sorted lexicographically), so that ciphertext cannot be moved to another session or used to impersonate another identity |
| Forward secrecy | Session keys come from one-time ephemeral keys and become invalid as soon as the connection ends. Even if the long-term identity private key leaks later, earlier session content cannot be decrypted |
| Key rotation | The session key is renegotiated every 30 messages sent or received, and the switch is never one-sided (the initiator switches only after receiving the peer's acknowledgement); file transfers rotate the key once before and once after they start. Old keys are kept for a 5-second grace period to handle messages in flight, and are then destroyed |
| Protocol version check | If the protocol versions of the two handshake peers differ, the connection is refused outright |

### 3. Tamper and replay resistance

| Protection goal | Implementation |
| --- | --- |
| Integrity check | The GCM authentication tag is verified on decryption; on failure the message is judged to have been tampered with, discarded and the connection dropped |
| Replay resistance | The 96-bit nonce consists of a 4-byte random session prefix and an 8-byte monotonic counter; the receiving end keeps a sliding window of 4096 entries and discards any duplicated or out-of-range historical message |
| Session isolation | Every connection has an unrelated session ID and keys, so the ciphertext of one session cannot be used in another |

### 4. Resistance to traffic analysis

| Protection goal | Implementation |
| --- | --- |
| Content length obfuscation | Before sending, zlib compression is applied as needed (used only when the size really shrinks, and the plaintext must be between 48 B and 64 KiB), then the result is rounded up to a multiple of 512 bytes and 0–511 bytes of random padding are added. Message length therefore has no stable correspondence with plaintext length |
| Chunked transfer | Files are sent in 256 KiB chunks. File chunks are neither compressed nor padded (padding stops once the plaintext exceeds 16 KiB), so that throughput is not affected |
| Resend traffic spreading | Session resends are carried out in batches (at most 80 per batch) with random intervals between batches, avoiding a fixed pattern of large traffic |

### 5. Access control and data retention

| Protection goal | Implementation |
| --- | --- |
| Two-way friend confirmation | A device that has not been accepted cannot send messages or files |
| Verification question | The answer is turned into a key with PBKDF2-HMAC-SHA256 (200,000 iterations and a random salt); only the HMAC proof travels over the network, and the answer itself is never transmitted; every question and answer carries a nonce to prevent replay. Reaching the limit of consecutive wrong answers (3 by default, configurable from 1 to 10) blocks the other side automatically |
| Blocking and removal | Contacts can be blocked, unblocked or removed at any time. Blocking rejects all of the other side's connections and requests, and removal notifies the other side |
| Invisible mode | With "Allow others to discover me" turned off, no broadcasts are sent and no probe requests are answered; a connection can only be started by the peer entering `IP:TCP port` manually |
| History never touches disk | Chat history is kept only in process memory and cleared on exit; no session record file exists on disk |
| History size limit | At most the latest 500 records are kept per contact; older records are discarded once the limit is exceeded |
| Resends do not downgrade security | The content resent after a disconnect is re-encrypted with the **current session key**, and old keys already destroyed by rotation cannot be used to recover historical content |

### 6. Protection boundaries (what is deliberately not done)

* **The fact of communication is not hidden.** An observer on the LAN can still learn who is communicating,
  when, and roughly how much data is exchanged, through traffic analysis.
  Length obfuscation only weakens the ability to guess content from message size; it does not hide the communication itself.
* **The first connection relies on out-of-band verification.** The program uses the TOFU (trust on first use) model.
  If the fingerprint is never verified through another channel, a man-in-the-middle risk remains in theory.
* **Endpoint security takes priority over protocol security.** If either device is compromised, or its identity
  private key leaks, every session that device takes part in can be read.
  The protocol cannot prevent leaks on the endpoint side.
* **Offline delivery is not supported.** There is no server, so when the other side is offline and this machine has no cached record, the message cannot be delivered.
* **The binaries are unsigned.** Antivirus software or SmartScreen may report them as false positives; you can build from source yourself.

## FAQ

| Symptom | Cause and handling |
| --- | --- |
| No device appears in the "Add contact" list | The other side has not started; the two sides are not on the same subnet; **the firewall has not allowed it** ("Private networks" must be ticked on first run); the other side has invisible mode enabled; or the two sides are on a mesh network such as Tailscale / WireGuard that does not forward broadcast — in that case use Add manually and enter the other side's IP |
| A friend request was sent but the other side saw no prompt | If sending fails, the program shows a dialog explaining the reason; if it was sent successfully, the other side needs to click "Accept" under "New friends" |
| The other side cannot be found, but its IP is known | Enter `their IP` at the bottom of "Add contact" (the port is probed automatically); if the other side has invisible mode on, enter `their IP:their TCP port` |
| Transfers break off, or the status stays at "Awaiting verification" for a long time | The other side has gone offline (the app reconnects automatically once they are back online); or the firewall is blocking the TCP connection |
| How to confirm there is no man in the middle | Both sides open "Settings" and compare whether their identity fingerprints match, and confirm this through another channel (face to face, over the phone) |
| Is chat history kept? | It is never written to disk and is cleared when the program is closed. Only while one side is still online does the other side receive that part of the conversation again when it comes back online |
| Antivirus software or SmartScreen raises an alert | The program is not code-signed, and PyInstaller-packed programs are occasionally reported as false positives; you can run it from source, or build it yourself as described below |

## Project structure

```
lanchat/                Core implementation
  discovery.py          UDP broadcast discovery and network adapter enumeration
  connection.py         Encrypted connections, key rotation, file transfer
  crypto.py             Key agreement, message encryption/decryption, length obfuscation
  identity.py           Identity keys, device identifier and fingerprint
  i18n.py               Multi-language framework (catalog lookup, system language detection, language normalization)
  locales/              Catalog folder: en.py (English), zh_hant.py (Traditional Chinese)
  protocol.py           Message encoding and decoding
  service.py            Session service and event dispatch
  gui.py                Tkinter graphical interface
  console.py            Console output encoding handling
  startup_log.py        Startup timing records and diagnostic logs
  winfocus.py           Window top-most and focus handling
chat_gui.py             Graphical interface entry point
chatgui.py              Alias entry point with the same name (also starts without the underscore)
tools/                  Development tools: i18n_extract.py (extract catalog entries), i18n_check.py (validate catalog files)
lanchat.spec            PyInstaller packaging configuration
使用说明.txt            Brief instructions shipped with the release package (Chinese)
使用说明.en.txt         Brief instructions shipped with the release package (English)
README.en.md            English version of this README
docs/开发文档.md        Design rationale, protocol, encryption details and fix records (Chinese)
docs/Development.md     The same, in English
```

## Development and build

**The dependencies come in two parts**: running and developing need only `cryptography`; **building an
exe additionally needs PyInstaller, which is not in `requirements.txt` and must be installed
separately** (people who only run from source never need it, so it is kept out of that file).

```bash
pip install -r requirements.txt        # runtime dependency: the only third-party library is cryptography
python chat_gui.py                     # start the graphical interface
python chatgui.py                      # same as above (alias entry without the underscore)
python chat_gui.py --self-test         # simulate both ends on one machine and verify the whole flow
python chat_gui.py --diagnose          # run the environment self-check only, without opening the UI
```

### Build steps (Windows, run them in this order)

Install PyInstaller in a **dedicated virtual environment** before building: this keeps your system
Python clean, pins the PyInstaller version (its major releases occasionally change packaging
behaviour), and prevents unrelated libraries installed on your machine from being pulled into the
bundle.

**① Create the build environment** (used only for packaging; you can delete and recreate it at any
time — `packaging_env\` is in `.gitignore` and never enters the repository)

```bat
python -m venv packaging_env
```

**② Install the dependencies** (both the runtime dependency and PyInstaller go into this environment)

```bat
packaging_env\Scripts\python.exe -m pip install --upgrade pip
packaging_env\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
```

**③ Build** (`--clean` removes the intermediate output left in `build\` by the previous run, so no
stale files can make the result inconsistent)

```bat
packaging_env\Scripts\pyinstaller.exe lanchat.spec --noconfirm --clean
```

**④ Verify the output** (`--diagnose` only runs the environment self-check and does not open the UI;
a windowed program shows no console output, so append `> out.txt` if you want to capture it)

```bat
dist\LanChat\LanChat.exe --diagnose
```

The result is in **`dist\LanChat\`** and it is a **folder build (onedir)**: double-click `LanChat.exe`
inside it and it starts in a second. **When sending it to someone else, copy the whole folder**
(do not copy the exe on its own — its dependencies live in the sibling `_internal\` folder). For the
single-file build with "only one exe", swap in the `EXE(...)` block given in the comment at the end of
`lanchat.spec` and delete `COLLECT` (the cost is that the first start has to unpack for 1~3 seconds,
and it may be blocked in some restricted environments).

> If PyInstaller is already installed on your machine, you can also just run
> `pyinstaller lanchat.spec --noconfirm --clean`, but a virtual environment makes problems far easier
> to reproduce.

Common command-line arguments:

| Argument | Description |
| --- | --- |
| `--name` / `-n` | Nickname; if left empty, a prompt asks for it at startup |
| `--port` | Local TCP port, 0 means automatic assignment (default) |
| `--discovery-port` | UDP auto-discovery port, default 50505 |
| `--data-dir` | Folder for identity and friend data |
| `--download-dir` | Download folder for received files |
| `--rekey-after` | Renegotiate the session key every how many messages sent or received, 0 means off, default 30 |
| `--lang` | Set the interface language: `zh-Hans` / `zh-Hant` / `en` (defaults to the system language) |
| `--auto-accept` | Receive files automatically without asking one by one |
| `--no-auto-connect` | Do not connect to added friends automatically |
| `--no-focus` | Do not force the window to the front |
| `--window-test seconds` | Show only one test window, used to confirm whether the graphical environment is working |
| `--version` | Show the version number |

For the design rationale, protocol format, encryption details and historical fix records, see [Development notes](docs/Development.md).

## License

This project is licensed under the [MIT](LICENSE) license.

English | [简体中文](开发文档.md)

# Developer documentation · Principles / Protocol / Tests / Fix log

> This is for developers and curious people: the complete protocol, the encryption details, the threading model, and the story behind every fix.
> If you only want to use it, [README](../README.md) is enough.

---



### Two ways to use it (packaged build / source)

```bash
# ① Use the folder build from the Release (no need to install Python)
LanChat\LanChat.exe

# ② Run from source (requires Python 3.10+ and cryptography)
pip install -r requirements.txt
python chat_gui.py                    # normal use
python chat_gui.py --self-test        # self-test mode: simulate two people chatting on one computer
python chat_gui.py --diagnose         # environment self-check only, without opening the GUI
```

> Repackaging (PyInstaller is not in the repository; just create your own virtual environment and install it):
> ```bash
> python -m venv packaging_env
> packaging_env\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
> packaging_env\Scripts\pyinstaller.exe lanchat.spec --noconfirm --clean
> ```
> `lanchat.spec` builds a **folder build (onedir)** `dist\LanChat\` by default: it starts fast, and you just double-click
> `LanChat.exe` inside it; when sending it to someone else, copy the whole folder over.
> For the single-file build with "only one exe": replace `EXE(...)` in `lanchat.spec` with the version given in the comment
> and delete `COLLECT` (the cost is that the first start has to unpack for 1~3 seconds, and it may be blocked in some restricted environments).


---

## Contents

<!-- TOC -->
1. [Get started in five minutes](#1-get-started-in-five-minutes)
2. [Self-test mode (verifiable on a single computer)](#2-self-test-mode-verifiable-on-a-single-computer)
3. [The interface](#3-the-interface)
4. [The complete flow of adding a friend](#4-the-complete-flow-of-adding-a-friend)
5. [Software principles](#5-software-principles)
6. [Encryption principles (compared with HTTPS)](#6-encryption-principles-compared-with-https)
7. [File transfer](#7-file-transfer)
8. [Data directory and persistence](#8-data-directory-and-persistence)
9. [Command-line arguments](#9-command-line-arguments)
10. [FAQ and troubleshooting](#10-faq-and-troubleshooting)
11. [Fix log](#11-fix-log)
12. [Project structure and tests](#12-project-structure-and-tests)
13. [Internationalization (i18n)](#13-internationalization-i18n)
<!-- /TOC -->

---

## 1. Get started in five minutes

First launch:

1. The interface first asks you to **Set nickname** (used to tell who is who during chats);
2. At the same time it generates an **identity key pair** (Ed25519) on this machine and saves it to `~/.lanchat/identity.json`;
3. The interface shows your **identity fingerprint** (for example `AB3F-91C2-77DE-0A55`) — this is your "ID number".

Then: click **➕ Add contact** to see the people found automatically on the LAN → select one and click **Send friend request** →
the other side clicks **Accept** under **🔔 New friends** → both sides automatically establish an encrypted session → start chatting.

> The packaged build is just a double-click on **`dist\LanChat\LanChat.exe`**; to run from source, mind the underscore in the file name:
> **`chat_gui.py`** (`chatgui.py` is an equivalent alias file).

## 2. Self-test mode (verifiable on a single computer)

```bash
python chat_gui.py --self-test
```

It starts **two complete, independent instances** on this machine ("A" and "B"), and **each instance has a window
exactly like the one in normal use**: contact list, Add contact, New friends, context menu, Settings and file transfer are all there.
You click "➕ Add contact" in one window to send a request and "Accept" in the other; this is the real flow.

* Each instance has its own identity key, friend list, TCP port and data folder; they do not affect each other;
* The two windows are placed **side by side** on the screen, so they do not cover each other;
* **Nothing is preset and nothing is done for you**: to try the verification question, right-click the other side in one window → "Set verification
  question", then send a request and answer it in the other window; to try blocking, block them yourself. What you see is exactly
  the same as two computers chatting. The friend request appears under `🔔 New friends (1)` in **the other window**.
* The two windows discover each other **through real broadcast** (same as two computers), and the program does **not** register the other side's address for you in advance;
  after startup it waits up to 9 seconds, and falls back to registering once only if broadcast really does not work (in that case chat / file transfer still work, but "Invisible"
  shows no effect in self-test, and the window says so).
* The startup log prints the addresses and sources that the two instances see for each other
  (`Self-test mode: B(this device) as seen by A(this device) = 192.168.31.223:52344 (source: Broadcast discovery)`);
  a port of 0 or "Not registered" means the self-test environment has a problem (`--diagnose` checks it too).
* The self-test window has one extra button at the top, **"🧪 Disconnect for 6 seconds"** (self-test mode only): pressing it takes this window **really**
  offline for 6 seconds — it stops listening, stops broadcast (sends bye) and drops all sessions — and then comes back by itself. The other window first shows
  "The other side is offline", then reconnects automatically when the time is up; messages you send in the meantime are stored locally first and delivered as "new messages" once the other side is back.
  One button verifies the whole chain "disconnect → offline notice → automatic reconnect → resend/resend history".

To verify only whether the graphical environment can show a window, use `--window-test 15` (it closes automatically after 15 seconds).

## 3. The interface

```
┌──────────────────────────────────────────────────────────────────────┐
│ Me: Xiaoming  [⚙Settings] [📢Notifications①] [✋Answer question①] [➕Add contact] [🔔New friends③] │
│ Fingerprint AB3F-91C2-77DE-0A55                                      │
├────────────────────┬─────────────────────────────────────────────────┤
│ 🔍 Search          │  Xiaohong 🔒 Encrypted connection  Fingerprint … │
│ 1 friend           ├─────────────────────────────────────────────────┤
│  🔒 Xiaohong  (2)  │  [12:01:03] Xiaohong: Hello                     │
│  ○ Aqiang [Offline]│  [12:01:10] Me: Hi there                        │
│                    │  [12:01:20] 📥 Received file: D:\...\report.pdf │
│                    ├─────────────────────────────────────────────────┤
│                    │  [Input box                            ] [Send] │
│                    │                                [📎 File]        │
└────────────────────┴─────────────────────────────────────────────────┘
```

* Markers on the left: `🔒` encrypted connection, `🔑` channel established but not friends yet, `○` friend offline,
  `…` waiting for the other side to verify, `🚫` blocked / `[Blocked by the other side]`; an offline friend is written plainly as **`[Offline]`**,
  and a verification question adds `🧩`. `(2)` is the unread message count; the search box filters by nickname/fingerprint.
* Top right `📢 Notifications`: **"the other side removed you / blocked you / cancelled the request / came online / went offline" all appear here**,
  and when there are unread items it turns orange and shows the count; important events (removed, blocked) also pop up a dialog, so you never just see "the person suddenly gone".
  Double-clicking a notification jumps to the corresponding person.
* Top right `✋ Answer question (①)`: when someone sets you a verification question it turns orange and shows the count; click it to answer.
* On the right: the other side's messages are on the left and mine are on the right; received files are blue links (click one to open its folder).
* **Right-click a contact**: Send file / Reconnect / Cancel friend request / Set verification question / Remove friend / Block this user / Unblock.
* Press Enter to send, Shift+Enter for a new line.

## 4. The complete flow of adding a friend

```
        Xiaoming                                Xiaohong
  ① Click "Add contact" to see Xiaohong (UDP broadcast discovers the people who are online)
  ② Click "Send friend request"  ──────────►    ③ If Xiaohong set a verification question: she first sets Xiaoming a question
  ④ Answer in the dialog (answering wrong up to the limit -> Xiaohong blocks automatically; only a correct answer continues)
  ⑤ The request appears under Xiaohong's "🔔New friends" (with the other side's fingerprint/message)  ──► click "Accept" / "Decline" / "Block"
  ⑦ They become friends, an encrypted session is established ◄──────────    ⑥ After accepting, an encrypted session is established automatically at once (or once the other side comes online)
  ⑧ Two-way chat and file transfer (all encrypted with AES-256-GCM)
```

* **Before accepting**: they are just a "stranger / awaiting verification", and messages cannot be sent. Even if the encrypted channel is already established,
  only one kind of metadata — "friend request" — is allowed through; chat content is always rejected.
* **Decline**: the requester receives "The other side rejected your friend request" and can apply again directly (this is not blocking).
* **Remove friend**: removing **notifies the other side**, and their side also removes this contact and shows
  "xxx removed you from their friends". If you only dropped the connection without notifying, the other side would think you were still friends and keep reconnecting,
  and none of the messages sent would actually arrive (users reported this, fixed).
* **Block**: requests and connections from the other side are rejected at the **handshake stage**; under "⚙ Settings → Block list"
  you can **Unblock** at any time, and afterwards they can apply again.
* **Verify identity**: before accepting, you can open "Settings" and compare both fingerprints (the same idea as the "security code" in WeChat/QQ);
  matching fingerprints mean nobody is impersonating them.

### 4.1 Friend request verification question (optional)

The rule is simple: **whoever is to be added sets the question**. Xiaohong sets the question → when Xiaoming comes to add her he must answer correctly; after reaching the wrong-answer limit he is auto-blocked by Xiaohong.

* **Setting a question**: in "➕ Add contact" Xiaohong selects the LAN user "Xiaoming" and fills in "question + answer + wrong-answer limit",
  then clicks **Set a question for the selected person**; you can also right-click an existing contact on the left → "Set verification question".
  **The person who set the question can change their mind at any time**: on the same screen click **"Cancel question"** (it shows the question currently set for them),
  or right-click the contact → "Set verification question" → "Cancel question". After cancelling, the other side can apply again without answering.
* **Answering**: after Xiaoming clicks "Send friend request", **an answer window pops up automatically**; it does not matter if it is closed,
  you can continue answering at any time by clicking **✋ Answer question** in the top right or **✋ Answer question** in "Add contact"
  (**no need to select anyone in the list first**).
* **A wrong answer is known on the spot**: after submitting, the popup **does not close**, and once the other side has verified it, it immediately shows
  "❌ Wrong answer, N attempts left" in the popup — just edit it and click "Resubmit"; a correct answer shows
  "✅ Correct answer" and closes the window automatically. If the other side never replies (they may have dropped offline), you are told as well.
* **Do not want to answer / the other side has gone offline**: the answer window has **"Cancel friend request"**, and the top-right menu (right-click the contact) has
  a menu item with the same name. After clicking it, the question on the other side becomes void and the connection is closed; to add them later, just send a new request.
* **Wrong answers**: how many attempts are left is written in the chat window; after the limit is reached Xiaohong auto-blocks Xiaoming.
  After Xiaohong unblocks him, Xiaoming **clicks "Send friend request" once more** and receives a **new question** that he can answer again.
* **Correct answer**: only then does Xiaoming's request appear in Xiaohong's "🔔 New friends", marked "passed the verification question".
* **A question is stored per person and does not follow the friendship**: if Xiaohong removes Xiaoming (or rejects him), Xiaoming **must answer again**
  when he adds her back; if Xiaohong changes the question, those who answered correctly before must answer again too. Only "still friends" means no repeated answering.

Security properties:

* **The answer never goes online and is never written to disk in plaintext**; on disk there is only `salt + derived key K = PBKDF2(answer, salt)`; on the wire there is only
  "the question + a random number + a one-time proof"; the plaintext answer is not even kept in memory;
* Every question uses a **fresh random number**, so the same answer yields a different proof each time and a packet capture **cannot be replayed**;
* Verification computes the proof directly with the K persisted to disk, so **the program can still judge correctly after a restart** (older versions kept the answer only in memory,
  so after a restart they treated people who had answered correctly as wrong, all the way to an automatic block — this is in the fix log);
* Bypassing this (brute-forcing the answer offline) costs **200,000 PBKDF2 iterations per candidate**, and online brute force is shut down outright by the wrong-answer limit;
* Verification uses the constant-time comparison `hmac.compare_digest`, avoiding a timing side channel.

Protocol: after the question-setting side saves K, it sends `question-challenge{question, salt, nonce, max_attempts}`;
the answering side computes `proof = HMAC(K, "lanchat/v2/answer-proof" | nonce)` and returns `question-answer{proof}`;
the question-setting side computes the same proof with the stored K and compares. See `lanchat/crypto.py` for the
`answer_key / answer_proof / answer_proof_matches_key`.


## 5. Software principles

### 5.1 Overview

```
        ┌─────────────────── UDP broadcast (auto discovery) ────────────────────┐
        │ every 3s: who I am / name / my TCP port                               │
        ▼                                                                       ▼
        ┌────────────────────────┐                     ┌────────────────────────┐
        │  PC A (Xiaoming)       │◄── TCP encrypted ──►│  PC B (Xiaohong)       │
        │  UDP 50505 recv bcast  │ handshake/chat/file │  UDP 50505 recv bcast  │
        │  random TCP port       │                     │  random TCP port       │
        └────────────────────────┘                     └────────────────────────┘
```

### 5.2 Auto discovery (no need to enter an IP)

* Every machine listens on **the same UDP port** (50505 by default), binds `0.0.0.0` and enables address reuse,
  so several instances on the same machine can see each other as well.
* Every 3 seconds it sends a beacon to the **directed broadcast address** of its own subnet (computed from the NIC IP + subnet mask, probed cross-platform):

  ```json
  {"t":"lanchat","v":2,"k":"beacon","id":"lc-3f2a…","name":"Xiaoming","port":54021,"dport":50505,"ts":1730000000}
  ```

  `k:"who"` is a **unicast probe** ("are you there", see 5.2.1): the receiving side **replies by unicast** with a `k:"beacon"`,
  and the reply is sent to the `dport` declared by the probing side. `k:"bye"` means exit.

* On receiving someone else's beacon it is registered as "online"; after 12 seconds with no message it is marked offline; a rename is rebroadcast immediately.
* Because **broadcast does not cross subnets**, "can see it" means "on the same LAN", with no need to enter an IP by hand.
* The TCP port used for chatting is assigned randomly by the system (the beacon carries the real port), avoiding port conflicts.

**Which networks support auto discovery** (the key is whether that network carries layer-2 broadcast):

| Network | Auto discovery | Notes |
| --- | --- | --- |
| Same LAN / Wi-Fi, several instances on one machine | ✅ | |
| Radmin VPN, Hamachi, ZeroTier (default L2 mode) | ✅ | All are virtual layer-2 networks, and broadcast is forwarded within the same virtual network |
| OpenVPN **tap**, Hyper-V/VMware host-only network | ✅ | Also layer 2 |
| **Tailscale, bare WireGuard, OpenVPN tun, Netbird** | ❌ | A layer-3 tunnel **has no concept of broadcast**; only "Add manually" below can be used |
| Different subnets across routers / VLANs | ❌ | Broadcast does not cross routers; this is by design |
| Wi-Fi with "client isolation / AP isolation" enabled, guest networks | ❌ | The intermediate device simply drops the broadcast |
| Any network, but the firewall blocks UDP 50505 | ❌ | A VPN adapter is often classified as a "public network"; on first run you must click "Allow" |

The program computes a separate directed broadcast address for **each network adapter** (not only the default-gateway one). With
`python chat_gui.py --diagnose` you can see the actual "local address / broadcast address" it computed, and tell at a glance whether that virtual adapter was included.

### 5.2.1 Manual mode (when broadcast does not work)

At the bottom of "Add contact" there is **Add manually**, with two ways to fill it in:

| What you enter | What the program does | When to use it |
| --- | --- | --- |
| **IP only** (`100.64.0.7`) | First sends a **UDP unicast probe** asking the other side "are you there"; the other side **replies by unicast** with a beacon carrying its **current random TCP port** → connect straight to it | Default; neither side **needs** a fixed port |
| `IP:TCP port` (`100.64.0.7:50606`) | Skips the probe and connects straight over TCP | The other side has "Invisible" on, or the other side changed the discovery port |

The unicast probe is sent to the other side's **discovery port**: first the one configured on this machine, then additionally the standard `50505` (if both sides
change to the same custom port it matches as well). When the two sides' discovery ports differ completely, it cannot be probed and you fall back to entering `IP:TCP port`.

```
IP only:  you ──UDP unicast "who"──► peer:50505
          you ◄──UDP unicast beacon── peer               (carrying its real TCP port)
          you ──TCP connect────────────────► peer:that port   → encrypted handshake
```

Other key points:

* **Punctuation in the address is corrected automatically**: the Chinese colon `：` (U+FF1A) typed under a Chinese IME, full-width digits `５０６０６`,
  extra spaces, `192.168.1.5 50606` (space as a separator), even backticks pasted in together with it `` `IP:port` ``,
  and `http://IP:port/` are all normalized to `IP:port` before use (when a real cleanup happened, the chat area states "Address cleaned up automatically").

* With IP only, the probe phase already obtains the other side's **real identity**, so not even a placeholder id is needed;
  with a hand-entered `IP:port`, it is first registered as the placeholder `manual:IP:port` and automatically migrated to the real identity after the handshake
  (`_adopt_real_identity`).
* A hand-entered address is **persisted to disk** (`manual_address`), and it can still reconnect as before after a restart.
* Keeping the connection alive is the **adding side's** responsibility: what the added side receives is an inbound connection, and that source port is ephemeral and cannot be used to dial back
  (so the program does not store it as the other side's address either). To let the other side also connect to you on their own initiative, have them add you manually once
  (at that point they need your IP:TCP port).
* Networks where broadcast works **do not need** this section at all.

### 5.2.2 Invisible (not discoverable automatically)

⚙ Settings → **Allow others to discover me**; once it is turned off:

* **No beacon is broadcast** → others cannot see you in "People on the LAN";
* **Unicast probes are not answered** → others entering only an IP cannot find out your port either;
* Others can only add you by **"Add manually" with `your IP:your TCP port`** (the port is visible in Settings;
  it is a good idea to pin one under "Local port" as well so that it does not change after a restart);
* You yourself **can still see others** (you still receive broadcasts) and chat normally — it is "I can find others, others cannot find me".
* The status bar shows **🕶 Invisible**, so you always know which state you are in.

> **Self-test mode can verify it too**: both windows of `--self-test` **have the discovery layer enabled**, and they really see each other
> **through broadcast** — at startup the program only waits for broadcast (at most 9 seconds) and does **not** pre-insert the other side into your list. So:
> turn off "Allow others to discover me" in window A, and within 12 seconds (offline timeout) A disappears from "People on the LAN" in window B; turn it back on and
> it comes back.
>
> If broadcast really does not work on the network (only a VPN adapter / the firewall blocks UDP), the self-test **falls back** after 9 seconds to the internal
> registry to make the two windows visible to each other, and writes in each chat area "broadcast does not work, fallback registration; **invisibility cannot be tested in this case**".
> Someone registered by the fallback shows **Manual entry** in the status column of the list — it is a different thing from "someone discovered".

### 5.3 Friendship is a separate gate

Discovery ≠ being able to chat. Relationship state is stored in `contacts.json`, with four kinds:

| State | Meaning | Can chat |
| --- | --- | --- |
| `friend` | Friend | ✅ Yes |
| `request-out` | I sent a request, waiting for the other side to confirm | ❌ No |
| `request-in` | The other side asked to add me, waiting for me to handle it | ❌ No (becomes friend after clicking Accept) |
| `blocked` | Blocked (the two directions are recorded separately) | ❌ No, and the blocked side's connection is rejected during the handshake, with an explicit `code=blocked` reason |

**Block ≠ Remove friend**: blocking only means "temporarily reject the other side's messages and requests", and `friend_before_block` remembers
"whether we were friends before the block". On unblocking: if you were friends you are **still friends** (auto-reconnect and a "Unblocked" notification to the other side);
if you were not friends it goes back to "stranger" so the other side has to apply again — it **never** creates a pending request that has never answered a question.
To disconnect completely, use "Remove friend" (the other side is notified).

The two directions must be kept apart: `blocked` is "I blocked them" (I reject), `blocked_by_peer` is "they blocked me"
(recorded only, and it must not be used to reject the other side's connections).

**Authorization comes from "the other side is in my friend list", not from some message** — that way, even after reconnecting and changing session keys,
the permission check cannot be bypassed.

All three kinds of "state change message" require an **acknowledgement** to count as delivered (see section 5.6):

| Message | Who sends | Acknowledgement | When no acknowledgement arrives |
| --- | --- | --- | --- |
| `friend-request` | The applicant | `friend-request-ack` | Resend every 5 seconds (up to 6 times, then switch to a 30-second routine redial) |
| `friend-accept` | The accepting side | `friend-accept-ack` | Resend on every session creation/change until confirmed |
| `friend-unblocked` | The unblocking side | `friend-unblocked-ack` | Try once immediately (dial if there is no channel), then retry every 5 seconds |

These states are all persisted to disk (`accept_pending` / `unblock_notice`), so **it does not matter if the other side is offline**: they are resent as soon as it comes online.

### 5.4 Thread model

```
Main thread / GUI : UI, buttons, event dispatch (all network events are delivered in through a queue)
tcp-accept        : Accepts TCP connections, starting one handshake thread per connection
discovery         : One thread receives UDP broadcasts and periodically sends its own beacon
conn-manager      : Sweeps every 0.5 seconds: reconnects offline friends (with backoff), resends unfinished friend requests
3 threads per session : rx (receive + decrypt + dispatch), tx (take ciphertext from the queue and write it to the socket), heartbeat (heartbeat)
```

Key constraints (all of these were learned the hard way, see [Fix log](#11-fix-log)):
**Within one session, plaintext → encryption → enqueue must complete under the same lock**, and **all frames (including heartbeats) must go through the same
send queue / the same writer thread**, otherwise the ciphertext is reordered and the peer's nonce check judges it a replay outright.

### 5.5 The journey of one message

```
Enter pressed in the input box
  → ChatService.send_text()         checks that the other side is a friend and the session is authenticated
  → Connection.send_message()       inside _order_lock: serialize JSON → AES-256-GCM encrypt → enqueue
  → writer thread protocol.send_frame()    {"type":"enc","body":{"n":nonce,"c":ciphertext}} + "\n"
  → network                         (a man in the middle can only see base64 ciphertext)
  → peer read thread read_frame()   frames by "\n"
  → Connection._dispatch()          decrypts with the session key + checks the nonce counter
  → _handle_message()               classifies: chat / file request / file chunk / friend event
  → ChatService emits a ServiceEvent  through the queue to the GUI thread
  → the UI renders the bubble + unread +1
```

### 5.6 Frame format (for packet-capture debugging)

Plaintext frames (they appear only during the handshake and heartbeat phases):

```json
{"type":"hello","v":2,"suite":"X25519-ED25519-HKDF-SHA256-AES256GCM","sid":"…",
 "purpose":"friend","card":{…identity card…},"x":"ephemeral public key","nonce":"…"}
{"type":"proof","sig":"…"}          {"type":"error","reason":"…","code":"blocked"}
{"type":"ping"}                     {"type":"pong"}
```

The `code` of `error` is an optional machine-readable reason (so far only `blocked` is used): the peer can use it to display clearly
"you are still on my block list", instead of a vague "cannot connect". After writing this frame, the write direction is half-closed first
(`SHUT_WR`) and any data the peer may already have sent is drained, and only then is `close()` called — otherwise the
RST sent by `close()` flushes this frame away along with it, and the other side only sees "The other side disconnected early".

Encrypted frames (**all** content after a session is established):

```json
{"type":"enc","body":{"n":"nonce(base64)","c":"ciphertext(base64)"}}
```

The plaintext inside `body` is application-layer messages: `{"t":"chat","text":"...","seq":which message}`, `{"t":"file-offer",...}`,
`{"t":"file-chunk","body":{...}}`, `{"t":"friend-request","...}`,
`{"t":"friend-unblocked",...}` (unblock notification from the other side), `{"t":"history-state"/"history-replay",...}`
(reconciliation and resending of this session's chat history, see 5.7), and so on. A session whose friend request is not yet confirmed admits only
the few "talking about adding friends" types (`connection._PENDING_ALLOWED`); everything else is rejected.

**Length obfuscation is applied before encryption**: a `p` field of random length is inserted into the plaintext so that the ciphertext length falls into fixed buckets
(see 6.5); after decryption this field is dropped, which is why it is not visible in the message formats above.

The few messages that "must be acknowledged" (`…-ack`): `friend-request` / `friend-accept` /
`friend-unblocked`. They are all **idempotent**, and when no acknowledgement arrives they are resent at fixed intervals — a session being
deduplicated and torn down the instant it is established, or the other side being offline right then: in these cases "sent" does not equal "received by the other side".

### 5.7 Chat history: kept for this session + resent on reconnect (gone once you close)

Chat content is **not persisted** (deliberately: you chose "burn after closing"). But while the program is running it no longer lives only in that
text box in the UI — the service keeps one in-memory record per person (the most recent 500 per person), so:

* **Switch to another contact and back**, and the conversation you just had is still there (it used to be cleared);
* **One side restarts while the other is still running** → the side that is still running **resends** that conversation back, and the restarted side's chat area
  shows the earlier conversation again (even "what you yourself said at the time" is restored, because the other side still holds it);
* **Messages sent while the other side is offline are not simply lost**: they are recorded locally first, and resent together with the history once the other side comes online;
  among them, those "that were not delivered at the time" **count as new messages** on the other side (with an unread indicator), while pure history is only resent without a notification.

The reconciliation protocol (all encrypted frames, not exchanged before the identity is confirmed):

```
Channel established (both sides are friends) → each sends  {"t":"history-state","in":which message of yours I received,"out":which message I sent up to}
Receiving the other side's reconciliation → I send back the pieces "they are missing" from my side, framed
                        {"t":"history-replay","entries":[{"from":who,"seq":which message,"ts":time,"text":content,
                                                          "pending":true(included only when it was not delivered at the time)}]}
Duplicates/reconnects do not display twice either: entries less than or equal to the "known sequence number" are discarded outright.
```

**Resending does not need the "old key"**: what is stored in memory is **plaintext**, and when resending it is re-encrypted with the **current** session key
(plus length obfuscation on top). So the "discard after use" rule of key rotation is completely unaffected — only new ciphertext ever travels over the
network from start to finish, and not a single frame is a product of an old key.

Boundaries (all written in the code comments):

* Only the **one side** that is online has that record — if both sides exit, the conversation is gone completely (this is the design goal);
* What is resent is **text**; files are not retransferred (retransferring large files is inappropriate, and the files themselves have already been saved to the download folder);
* If the other side's program is killed / the network drops while a message is in flight, that one message may be missing on both sides — the record is "best effort" and does not promise server-like reachability.

## 6. Encryption principles (compared with HTTPS)

| HTTPS / TLS | This tool |
| --- | --- |
| The ECDSA key inside the server certificate | The Ed25519 **long-term identity key** generated at startup (persisted to disk) |
| The ECDHE ephemeral key during the handshake | A fresh X25519 **ephemeral key pair** generated for every connection |
| The CertificateVerify signature | An Ed25519 signature over the whole handshake transcript (both sides' ephemeral public keys + identity card + session ID) |
| Finished / key derivation | **HKDF-SHA256 → bidirectional AES-256-GCM** session key |
| Certificate fingerprint verification | When adding a friend, the other side's **key fingerprint** is displayed and recorded, so it can be checked manually |

### 6.1 Handshake sequence (repeated on every connection)

```
Xiaoming                                                Xiaohong
 │ hello: session ID + ephemeral X25519 public key + nonce + my identity card  │
 │────────────────────────────────────────────────────────►│
 │ hello: the same content (including Xiaohong's identity card)               │
 │◄────────────────────────────────────────────────────────│
 │ ① Each side verifies the identity card's self-signature, and peer_id == SHA256(public key) │
 │ ② ECDH(my ephemeral private key, the other side's ephemeral public key) → shared secret      │
 │ ③ HKDF(shared, salt=session ID+handshake digest) → 64 bytes → bidirectional keys             │
 │ proof: sign the handshake digest with the long-term Ed25519 private key                     │
 │◄───────────────────────────────────────────────────────►│
 │ ④ Mutual signature verification passes → everything afterwards goes as AES-256-GCM ciphertext │
```

### 6.2 Key derivation chain

```
Identity: Ed25519 long-term key pair (persisted to disk)    Per connection: X25519 ephemeral key pair (discarded after use)
                     │                                │
                     │ sign the handshake digest      │ ECDH
                     ▼                                ▼
             ┌──────────── handshake transcript ─────────────┐
             │ hello_I{card,x,nonce} + hello_R{card,x,nonce}  │
             └───────────────────────┬────────────────────────┘
                                     ▼
       shared = X25519(ephemeral private key, peer ephemeral public key)    ← the source of forward secrecy
                                     │
       HKDF-SHA256(shared, salt=SHA256(sid‖transcript), info="lanchat/v2/session-keys")
                                     ▼
        64 bytes → split into two 32-byte keys (initiator→responder / responder→initiator)
                                     ▼
        AES-256-GCM, nonce = 4-byte random session prefix ‖ 8-byte incrementing counter,
        AAD = sid ‖ the sorted peer_id of both sides   (binds session and identity, preventing ciphertext from being moved elsewhere)
```

### 6.3 Key rotation (KeyUpdate, every 30 messages by default)

The session key is not "used once per connection until the end": every `--rekey-after` (default 30) messages sent or received trigger a new round.
That way, even if some man in the middle later breaks one round's key, **they can only decrypt that one short stretch of the conversation**.

**File transfer performs two extra rounds**: at the moment the other side **accepts receiving**, a new key is
generated (this long stream of file data does not use the chat key), and after the file **finishes transferring**
(once the receiver has verified SHA256) another new key is generated, retiring the round used for the file transfer
outright. So the event "a large file was transferred" affects at most that one key round, and the chat before and
after it uses different keys. Either side can initiate this request (`rekey-request`): the actual rotation is still
performed only by the fixed "initiator", otherwise the two sides would each derive from their own nonce and drift apart.

```
Initiator                                  Responder
 │ begin(fresh): encrypted with the [old] key  │
 │───────────────────────────────────────►│  On receiving begin: switch to the new key immediately
 │ keep sending messages with the old key (in flight, arriving later) │ reply accept with the [old] key
 │◄───────────────────────────────────────│
 │ On receiving accept: switch to the new key  │
 │ (the old key is kept for a 5-second grace period, solely to decrypt in-flight messages) │ (a grace period is kept as well)
```

* The new key is derived with `HKDF-SHA256(shared secret ECDH, salt=session ID+round+fresh, info=role)`,
  **both sides compute it themselves; not a single byte of key material is sent over the network**;
* Both sides use the same `fresh` and the "role" label to distinguish directions (`i2r` / `r2i`), so the results at the two ends are complementary by construction;
* After the switch the old key is void immediately (only a 5-second grace period remains to handle in-flight messages);
* You can change the message count at any time under "⚙ Settings → Key rotation", or enter 0 to turn it off.

This protocol has three hard rules, and every one of them was bought by stepping on a pitfall (violating any one of
them makes the two sides' keys drift apart, after which every frame fails "ciphertext authentication failed" and the connection drops):

| Rule | Why |
| --- | --- |
| On receiving `accept`, this rotation **must be finished** (clear `_rekey_outstanding`) | Otherwise the state machine jams: after one or two rotations it never rotates again, and finally the 10-second timeout triggers a "one-sided key change" |
| **Never change keys one-sidedly**: switch only after receiving the other side's `begin`/`accept` | A one-sided switch = the other side is still on the old key, and the two sides drift apart at once. If the receipt has not arrived, **resend `begin`** (idempotent) instead of switching by yourself |
| Automatic rotation is initiated by **one side** only (decided by peer_id, with the same algorithm on both sides) | If both sides `begin` at the same time, each derives a different new key from its own `fresh`, drifting apart directly |
| In-flight **file chunk content** also needs the old key as a fallback | Chunks are "content encrypted a second time + an outer envelope"; if only the envelope gets the fallback, a large file being transferred at the moment of the key change is interrupted |

`tests/test_rekey.py` now verifies: for 40 consecutive messages **every single rotation really happens**
(not just one or two rotations), all sequential content is correct, epoch stays consistent when both sides blast
each other at the same time, and **a 2MB file transferred during frequent rotation has a matching SHA256**;
`tests/test_self_test_mode.py` additionally verifies "sending a 140KB and a 12MB file back to back does not drop the connection".

### 6.4 Overview of security properties

| Threat | How it is handled |
| --- | --- |
| Packet capture snooping on chats/files | Everything is AES-256-GCM ciphertext; chat and file chunks are both encrypted; at the network layer there is only `type=enc` |
| Impersonating someone | `peer_id = SHA256(Ed25519 public key)` cannot be forged; the identity card carries a self-signature; in a friend session peer_id must match the record |
| A man in the middle replacing the ephemeral public key | Both sides sign "the two ephemeral public keys + both identity cards" as a whole; changing any one item fails signature verification |
| Decrypting history after an old key leaks | **Forward secrecy**: the session key comes from one-time ephemeral keys, discarded right after the handshake |
| One connection being monitored long-term | **Key rotation**: a new round every 30 messages, so breaking one round only decrypts a short stretch |
| Strangers adding friends freely | **Verification question** (optional): only a correct answer gets you into the request list; wrong answers up to the limit trigger an automatic block |
| Replaying a captured answer to the verification question | Every question uses a fresh nonce, and only a one-time HMAC proof travels over the wire; neither the answer nor the derived key goes online |
| Treating someone who answered correctly as wrong after a restart | Verification uses only the derived key K persisted to disk (`answer_proof_matches_key`), and does not depend on the plaintext answer in memory |
| Replay / out-of-order old messages | A per-session random nonce prefix + incrementing counter + replay window (tolerates reordering, rejects duplicates) |
| Ciphertext being tampered with | The AES-GCM AAD binds the session ID and both identities; an authentication failure disconnects immediately |
| Forged identity cards | An identity card must be self-consistent (valid signature + peer_id matching the public key), otherwise it is rejected already during the handshake |
| Someone you want nothing to do with harassing you repeatedly | Blocking: rejected outright during the handshake, so the other side only sees "Blocked by the other side" |
| Oversized messages blowing up memory | An 8 MiB per-frame limit; files are streamed in 256 KiB chunks; the whole file's SHA256 is verified after receiving |
| **Guessing what you are doing from "how big the packet is"** | **Length obfuscation** (on by default): the plaintext is randomly padded to 512-byte "buckets" before encryption (see 6.5), so "a three-word chat" and "half a screen of text" produce ciphertext lengths in the same one or two size brackets, and two sends of the same content also differ in length |

### 6.5 Length obfuscation (padding) + compression: packet size must be hidden too

The ciphertext length is basically equal to the plaintext length, so "cannot decrypt the content" does not mean
"cannot tell what you are doing": **a few words of chat**, **a large batch of replayed history**, **a long file
name** all produce completely different packet sizes, so anyone capturing packets can classify them at a glance
and can even guess how many characters you typed. Therefore two processing steps are done before sending (both before encryption):

```
plaintext {"t":"chat","text":"hello","seq":1}
 ① compress as needed: repeated content is shrunk with zlib (used only when it pays off)   → for small messages this step is skipped
 ② random insertion: pad up to a multiple of one step (512B), then **randomly** add 0 to a full step of bytes
ciphertext ≈ the length above → whoever captures packets only sees "a packet of a few hundred to just over a thousand bytes", and **different every time**
```

* The key is "**random insertion every time**" rather than "always padded to the same size": send the same message 20 times in a row and nearly 20
  different lengths appear on the wire (measured 18/20); a fixed length is itself a fingerprint;
* Messages within the same step (1 character to 200 characters) differ in ciphertext size by only tens to hundreds of bytes (measured window 288 bytes),
  so it is impossible to tell exactly how many characters you typed;
* The padding content comes from `os.urandom` and is encrypted and authenticated together with the body by AES-GCM — it is unpredictable, and changing a single byte fails verification;
* The decrypting side automatically drops the padding and decompresses (`crypto.decrypt_message`), so the upper layer gets exactly the same message as before;
* Plaintext over 16 KiB (the file chunk case) is **neither padded nor compressed**: padding would not hide "I am transferring a large file", and compressing would waste CPU;
* History replay does two additional things: **at most 80 entries per frame**, and a **random 50–250 millisecond interval** between frames, sent on a background thread,
  rather than "one big burst" — otherwise a dense stream of large packets is itself a sign saying "history is being synced".

**Could compression leak instead?** No (this does not repeat CRIME/BREACH): that is a length oracle that appears only
when "content injected by the attacker and the secret are placed in the same stretch and compressed repeatedly";
here it is **per-message independent compression**, and a single message is written by one side only, so an attacker
cannot stuff their guess into the same compression stream.

**What it cannot stop (important)**: padding can only round up, so it **cannot hide the total volume and timing** — an
observer still knows "you two are communicating" and "about N KB was transferred just now". Truly hiding even that
would require constant-rate padding (always sending junk traffic), which is far too expensive for a small LAN tool.
So: **length obfuscation is "reducing classifiability", not "invisibility"**.

**Known boundaries**: adding a friend is a trust model of "manually confirming the fingerprint" — when establishing a
connection with a stranger for the first time, the identity is only presented as a fingerprint, which you have to
check yourself (the same idea as comparing security codes in WeChat). An attacker on the LAN can still perform
**traffic analysis** (knowing who is communicating with whom and roughly how much data was transferred); length
obfuscation only defeats "guessing content from packet size". When the other side is **offline and there is no local
record**, the message cannot be sent (within this session the side holding the record resends it, see 5.7).

**A note on reconnecting after a drop**: after the connection is closed it reconnects automatically and **performs a
new handshake (new one-time keys)**, so reconnecting is itself a complete key replacement.

## 7. File transfer

```
Sender                                              Receiver
 file-offer (name/size/SHA256)                      ──────────────►  prompt dialog (or auto-accept)
                                                                  ↓ accept
 wait for file-accept                               ◄──────────────  file-accept
   ↓ data is pushed only after the acceptance arrives
 file-chunk × N (each chunk encrypted separately)   ──────────────►  write to file + compute SHA256 while receiving
 file-end                                           ──────────────►  close file → compare SHA256 → report the result
```

* **The transfer starts only after the other side agrees**, so a decline wastes no bandwidth (early versions pushed data while waiting; fixed).
* **A progress bar is shown during the transfer**: a progress bar appears below the input box and displays
  `Send x.png: 43% (5.4 MB/12.1 MB) · 1.2 MB/s · About 6s left`; when the transfer finishes it shows "completed + average speed" and then collapses automatically.
* **There is only one unified download folder** (changed under "⚙ Settings → My download folder", and it can be opened with one click); all received files
  are stored there. The dialog shown when a file arrives has three buttons —
  **"📥 Accept (save to my download folder)"**, **"📂 Save to another location…"** (switch elsewhere for this one time; the folder and file name are up to you,
  receiving starts as soon as you choose, and the next dialog opens in the folder you chose last time), and **"Decline"**. If the target already exists it is renamed automatically and never overwritten.
* The dialog is skipped only when **Auto-receive files** is ticked in "⚙ Settings" (the file goes straight to the download folder);
  in that case a line such as "received automatically xx → where it was saved" is also left in `📢 Notifications`. Self-test mode does **not** auto-accept by default.
* Chunks are 256 KiB; after the plaintext is encrypted and base64-encoded, a single frame is still far below the 8 MiB limit.
* Files with the same name automatically get `(1)`, `(2)` appended and never overwrite an existing file; file names are sanitized (path separators are stripped).
* After the file is written to disk the whole-file SHA256 is verified, and a mismatch reports an explicit "checksum failed".

## 8. Data directory and persistence

| File | Content |
| --- | --- |
| `~/.lanchat/identity.json` | This machine's long-term identity private key + nickname (deleting it = becoming a different person, and others have to add you again) |
| `~/.lanchat/contacts.json` | Friend list, pending requests, block list, unread counts, last message |
| `~/.lanchat/settings.json` | Auto-receive files, auto-connect friends, key rotation frequency, **my download folder**, the verification questions set for each person |
| `~/.lanchat/last-run.log` | Startup log (timestamped, for troubleshooting) |
| `~/Downloads/` | Received files (**by default they are placed in the system download folder**) |

**Chat content is the only thing that is "not written to disk"** (see 5.7): it is gone once the app is closed, and is kept in memory only while the app runs,
to replay this stretch of the conversation back after the other side restarts/reconnects. A long-term record that "survives closing" is deliberately not implemented for now.

The download folder defaults to the system **Downloads** folder; use "⚙ Settings → My download folder" to change it to anywhere (the change takes effect immediately
and is remembered for next time). In a read-only environment it degrades automatically: `~/Downloads` → `~/Downloads/lanchat` → the current directory →
the temporary directory, and says so in the startup log.

## 9. Command-line arguments

```
--name Nickname              Skip the nickname setup and enter directly
--self-test              Self-test mode (two instances on this machine chat with each other)
--no-discovery           Self-test mode does not send UDP broadcasts
--window-test sec         Only one test window is shown, closing automatically after N seconds
--diagnose               Only the environment self-check (dependencies / folders / ports / local addresses / whether a window can be created), without opening the UI
--discovery-port PORT    UDP discovery port (default 50505; everyone in the same group must use the same one)
--port PORT              Local TCP port (default 0 = assigned automatically; **in manual mode, when the other side has to enter you by hand,
                         pin a port here**, for example --port 50606)
--download-dir DIR      Download folder for received files
--data-dir DIR          Folder for identity/friend data
--auto-accept            Auto-receive files
--no-auto-connect        Do not auto-connect to friends
--no-focus               Do not force the window to the front
--version
```

## 10. FAQ and troubleshooting

### It runs, but "does nothing"? Do these 4 steps in order

```bash
python chat_gui.py --diagnose        # 1) environment self-check (includes a real test of "can a window be created and shown")
python chat_gui.py --window-test 15  # 2) show only one test window, closing automatically after 15 seconds
python chat_gui.py --self-test       # 3) self-test mode, verifying the whole flow on a single computer
type %USERPROFILE%\.lanchat\last-run.log   # 4) startup log (timestamped, shows which step it got stuck on)
```

| Symptom | Cause / fix |
| --- | --- |
| **It hangs for a few seconds after double-clicking the exe (or after filling in the nickname and clicking "Get started")** | Fixed: building the discovery layer used to run `ipconfig` synchronously on the **UI thread** (1.5~2.5 seconds in the packaged windowed program, only 0.04 seconds when run from source), so Windows painted the window as "not responding". That enumeration is now cached and is done only on a background thread (see Fix log 67). If it is still slow, check the startup log for `⚠ Enumerating network adapters took N seconds` |
| Under "Add contact" there is nobody at all | The other side has not started the app; you are not on the same subnet/Wi-Fi; Windows Firewall is blocking UDP — on the first run you must allow "Private networks" |
| You clicked "Send friend request" and the other side gets **no notification** | The request is sent asynchronously: if it cannot be sent within 2.5 seconds a dialog **pops up directly** telling you why (for example "the other side is offline"). The receiving side must open `🔔 New friends` to get "Accept/Decline". In self-test mode, make sure the registered address in the log is not `:0` |
| The buttons in the "Add contact" window are cut off | Fixed (the window sizes its height to its content, and the buttons reserve their place first). If you have changed the UI code, keep the order "pack the fixed blocks first, the list last" |
| It stays at "Awaiting verification" / cannot connect | The other side has not accepted yet; or the other side is offline (it reconnects automatically once online); or the firewall is blocking TCP |
| **The unread count (1) keeps showing while the conversation is open** | Fixed (Fix log 72): unread no longer accumulates while the conversation is open; new messages are counted again only after you switch away or close the window |
| The other side accepted but messages cannot be sent | Check the status next to the title: you can send only when it shows `🔒 Encrypted connection`; when it shows `Not connected`, wait for the automatic reconnect or right-click "Reconnect" |
| You answered the verification question incorrectly | Every wrong answer tells you how many attempts are left; at the limit the other side blocks you automatically. After the other side unblocks you, **clicking "Send friend request" once more** gets you a new question |
| You clearly typed an answer, but it says nothing was entered | Fixed (in self-test mode with two windows the Tk variable was bound to the wrong window, so the text was visible in the box but could not be read). Now the dialog also grabs focus first, and if the text did land in the chat input box it asks whether to take it as the answer |
| The other side is offline / you do not want to answer that question | Click **"Cancel friend request"** in the answer window (or right-click the contact → the menu item of the same name); the question on the other side then becomes void |
| The other side removed me / blocked me / went offline, and all I saw was "they are gone" | All of these now go into **`📢 Notifications`** (with an unread count), and being removed/blocked also **pops up a dialog directly**; an offline friend is marked **`[Offline]`** in the list instead of quietly disappearing |
| When a file arrives there is no way to change the save location | The dialog now has three large buttons: **Accept (save to my download folder) / Save to another location… / Decline**; the download folder is changed in "⚙ Settings" |
| A wrong answer to the verification question gives no feedback | The dialog now stays open and shows "N attempts left" in place, so you can edit it and submit again right there |
| No dialog appeared and the file went straight to the default folder | That means "⚙ Settings → Auto-receive files" is on (off by default in self-test mode). Turn it off and you will be asked every time; while it is on, where the file was saved is still recorded in `📢 Notifications` |
| If I remove someone and add them back, do they have to answer the question again? | Yes. Verification questions are stored per person and do not follow the friendship; only while you are still friends do they not have to answer again |
| Running several instances on the same machine | That works: the TCP ports differ automatically; as long as `--discovery-port` is the same they can discover each other |
| Keeping two groups of people from interfering with each other | Have each group use a different `--discovery-port` |
| Starting over (changing identity) | Delete `~/.lanchat/identity.json` |
| Double-clicking the exe does nothing | First look at `%USERPROFILE%\.lanchat\last-run.log` (or the directory given by `--data-dir`); running `dist\LanChat\LanChat.exe --diagnose` from the command line shows the output |
| The two sides have different key rotation settings | They are negotiated during the handshake: if either side sets 0 there is no automatic rotation, otherwise the stricter (smaller) value is taken; changing the setting notifies the other side at once so that both take effect together |

## 11. Fix log

Real pitfalls hit during development, recorded in the order they were found (symptom → root cause → fix), which also makes regression checks easier later.

| # | Symptom | Root cause | Fix |
| --- | --- | --- | --- |
| 1 | Halfway through a file transfer, the receiver reports "chunk checksum failed / replay message detected" | The heartbeat `ping` bypassed the send queue and **wrote to the socket directly**, cutting in among the queued file chunks; the peer received ciphertext out of order and the nonce counter judged it a replay | Control frames also go through the same send queue/writer thread (`Connection.send_control`) |
| 2 | With concurrent sends, the peer occasionally cannot decrypt the ciphertext | In `send_message()`, "encrypt and take a nonce" and "enqueue" are two separate steps, so the heartbeat thread / file-stream thread / main thread interleave and ciphertext frames are **enqueued out of order** | Nonce allocation and enqueue go under the same `_order_lock`; the receiving side now uses a "replay window" (tolerates out-of-order, rejects duplicates) |
| 3 | After the receiver declines a file, the sender keeps pushing data | The sender starts pushing chunks as soon as `file-offer` is sent | Changed to wait for `file-accept` before transferring (`send_file` / `start_transfer`) |
| 4 | The upper layer never receives file request events | `_handle_payload()` did "return if the connection layer handles it itself" for `file-offer/file-end/file-accept` | Only `file-chunk` (high volume) is swallowed by the connection layer; everything else is reported to the upper layer |
| 5 | When both sides initiate a connection at the same time, the single surviving session is closed as well | The deduplication rule was asymmetric: `keep_outgoing = my_id < peer_id` means opposite things on the two sides, so both connections were closed | Symmetric rule: only when `my_id < peer_id` do you keep the connection you initiated; the other side drops the inbound one |
| 6 | Running `python D:\...\chat_gui.py` from another directory reported `ModuleNotFoundError: No module named 'lanchat'` | The entry point depended on the current working directory | The entry point inserts **the script's own directory** into `sys.path` |
| 7 | "Nothing happens" after launch: the Chinese Windows console raised `UnicodeEncodeError: 'gbk' codec can't encode '\u26a0'` | The program prints `⚠ ✅ 🔒`, but the Chinese console defaults to GBK encoding, so it **crashes right at the print** (when double-clicked it flashes by and you never see it) | Added `lanchat/console.py`; every entry point switches stdout/stderr to UTF-8 **before any print** |
| 8 | The `.bat` spewed `'灞€鍩熺綉...' is not recognized as an internal or external command` | cmd.exe read the UTF-8-encoded .bat as GBK, so Chinese comments/`echo` text turned into commands | The contents of `run_chat.bat` were changed to **pure English ASCII** (non-ASCII byte count = 0); the Chinese-named launcher keeps only a one-line ASCII forwarder |
| 9 | `run_chat.bat` said `Cannot find Python`, yet `python` clearly worked | The launcher relied only on `where python` | Try `LANCHAT_PYTHON` → `py -3` → `python` → `python3` → common install directories, in that order |
| 10 | **The window actually opened, but it was hidden behind the browser** (users thought "nothing happened") | Windows' foreground lock: a non-foreground process calling `SetForegroundWindow` is blocked by the system | Added `lanchat/winfocus.py`: `SetWindowPos(TOPMOST)` + `AttachThreadInput` + `SetForegroundWindow` + `FlashWindowEx` to flash the taskbar; the window is briefly topmost for 2.5 seconds |
| 11 | **Running `python chat_gui.py` (without `--name`) produced no window at all** | "Set nickname" was a `transient` Toplevel parented to a **withdrawn root**; it stayed `withdrawn / viewable=0 / 1x1`, while the main flow **waited forever** in `wait_window` for it to close | Show the main window first at startup, draw the nickname directly in the main window, and use `wait_variable` to wait for user input (the event loop is no longer blocked) |
| 12 | After closing the window Tk reported `invalid command name "..._drain"` | The `after` timer callback still fired after the window was destroyed | Save the job id and `after_cancel` it on close |
| 13 | After a disconnect, auto-reconnect **sometimes** stopped happening (a long wait, or it never recovered) | `_connect_once()` did an early `return` when "already connected / the other side just accepted the inbound connection", but never reset `dial_state` from `connecting`; the connection manager saw "connecting" and never retried | A `try/finally` guarantees that every exit path resets `dial_state` to `idle` |
| 14 | Automated tests could not read the startup log and wrongly concluded "the window was not shown" | Python's stdout is block-buffered, so the buffer contents were lost when the child process was `terminate()`d | `startup_log.step()` calls `flush()` explicitly after writing |
| 15 | After answering the verification question correctly, clicking "Accept" did not establish an encrypted session | The channel was not remembered when verification passed, so `accept_request` could not find a connection and had to wait for the next reconnect | Save `contact.connection` when verification passes; `accept_request` reconnects immediately when it finds no live channel |
| 16 | Reconnects kept failing with `Purpose mismatch (other side declared request)` | The handshake purpose (friend/request) was derived by each side's own state machine, so when the states were out of sync the two declarations disagreed and the handshake was rejected | One unified rule: "whoever wants to establish a session declares friend; accept as long as the other side is recognized" |
| 17 | After key rotation the two sides computed different new keys | Derivation used only "the old key for the local direction" | Switched to the shared ECDH secret plus a role label `i2r/r2i` |
| 18 | Messages in flight at the instant of a key switch were judged "replay" and the connection dropped | The peer had just switched to the new key while frames sent with the old key were still on the wire | Keep the old key for a 5-second grace period; frames confirmed to carry an expired counter are dropped silently |
| 19 | After a rotation, messages were stuck and never sent again | The send queue, the stale `_rekey_outstanding`, and the non-reentrant `_order_lock` were all waiting on each other | Switched to a standard KeyUpdate (switch on receiving begin, reply ack with the old key, the initiator switches after receiving the ack), `RLock`, and no more queueing |
| 20 | Editing source with PowerShell text commands turned all Chinese into mojibake | The default encoding of `Get-Content`/`Set-Content` round-tripped UTF-8 Chinese into mojibake that could not be restored | Rewrite the whole file with file tools; **rule: source code must never be round-tripped through a PowerShell text pipeline** |
| 21 | Self-test mode preset a `1+1=?` question and answered it automatically, which looked like "the program is putting on a show / has a bug" | To make the first experience "have an effect", the code preset the question and hint for the user and also kept hidden logic that "auto-answers questions you set yourself" | Self-test mode now **just opens two normal windows with no presets at all**; the hidden `_auto_answer` / `_my_questions` logic was deleted |
| 22 | While answering a question it said "select a person first" | The "Add contact" list was rebuilt by the broadcast refresh every 3 seconds, clearing the `Treeview` selection, so the person the user had selected "fell off" | The list uses `peer_id` as the `iid`, remembering and restoring the selection across refreshes; the "New friends" list is handled the same way |
| 23 | After a program restart, someone who answered correctly was judged wrong, all the way to an automatic block | The question setter kept the answer plaintext only in **memory** (`_question_answers`), so verification inevitably failed after a restart | Verification now uses a **persisted derived key** `K` (`answer_proof_matches_key`): the answer plaintext never goes to disk or into memory, and answers are still judged correctly after a restart |
| 24 | After being blocked by the other side you could never apply again (always "purpose mismatch") | ① "I blocked them" and "they blocked me" shared a single `blocked` flag, so the requester also blocked the retries that followed the other side's unblock; ② after unblocking, the two state machines (request-in vs request-out) made the handshake purpose declarations inconsistent | Added `blocked_by_peer` to distinguish the direction (re-applying after the other side unblocks automatically clears the local flag); the service-layer handshake now tolerates a purpose mismatch (authorization looks only at the friend list) |
| 25 | After one wrong answer you never received the question again, with no chance to retry | The dialog's "show each person only once" memory (`_answered_challenges`) permanently suppressed later questions | Deduplicate by **the nonce of each question** instead: the same question is not shown twice, and a fresh question from the other side (new nonce) pops up again; after the dialog is closed you can still click `✋ Answer question` to continue |
| 26 | **In self-test mode, "a friend request was sent, but the other side showed no notification at all"** | Self-test mode registered the peer address as soon as the instances were built, but that was **before** `service.start()` — the TCP port was still 0 at that point (the port is bound only inside start()), so the person showed up in the list and the status even became "Request sent, waiting for confirmation", yet the request could never be sent, and the reason was written only in the bottom status bar | Self-test mode now **calls `start()` first and registers addresses afterwards**; `register_manual_peer` errors out immediately when the port is ≤ 0 (no longer registering a fake address that is "visible but unreachable"); the startup log prints the real addresses registered on each side, and `tests/test_startup_window.py` guards this with real subprocesses |
| 27 | The bottom buttons in "Add contact" were half cut off by the window edge and could not be clicked | The dialog hardcoded `680x580`, and adding the "verification question" section made the content taller; since the bottom buttons were `pack`ed last, they were **squeezed out first** whenever the window was too short | Fixed-height sections (bottom button bar / hint / question-setting area / answer area) are all now `pack(side="bottom")` first, with the stretchy people list placed last; the window size adapts to its content with a screen-size fallback; a new automated check requires the bottom buttons to lie fully inside the window |
| 28 | **Right after a small file finished, sending a large file (12MB) immediately produced "Ciphertext checksum failed: ciphertext authentication failed" and dropped the connection** | The KeyUpdate state machine's `_rekey_outstanding` **was never cleaned up**: after the first rotation it stayed true forever → no further automatic rotations, while the 10-second timeout branch **switched keys unilaterally** (over and over, at that), so the two sides' keys diverged from there; file chunks are double-encrypted, and chunks in flight at the instant of a key switch had no old-key fallback | ① Clean up when `accept` is received / the peer's `begin` is adopted; ② if no ack arrives, only **resend begin** (idempotent), never switch keys unilaterally; ③ when a `begin` from the previous round arrives again, send the ack once more using the old key; ④ automatic rotation is initiated only by the side with the smaller peer_id, ruling out "both sides rotating at once, each with its own fresh"; ⑤ chunk contents get an old-key grace-period fallback |
| 29 | The key-rotation tests "looked green", but rotation had actually stopped long ago | The old tests only asserted `rekeys_done > 0` and that the `epoch`s matched: both sides being stuck in the same round satisfied that just fine | The assertions now require that "every single rotation really happens" (40 messages / every 5 ≈ 8 times), that bombarding both directions at once keeps the epoch advancing, and that a 2MB file transferred during frequent rotation passes SHA256 verification |
| 30 | **After removing a friend, the other side has no idea**: they still show you as a friend and keep reconnecting, yet not a single message gets through | `remove_friend` only `close()`d the connection and never told the other side "I removed you" | Added a `friend-removed` protocol message: send the notification before removing; on receiving it the other side removes the contact, closes the connection and shows an explicit notice |
| 31 | While answering a question, "I clearly typed the answer in" yet it keeps saying the answer is required | When the dialog had just appeared, Windows left the keyboard focus on the main window, so what the user typed went into the **chat input box** and the dialog stayed empty; the prompt in turn only said "please fill in the answer", which was baffling | After the dialog is shown, steal focus forcefully and select the whole input box; when the answer is empty but the chat input box has content, just ask "use this as the answer?"; the prompt text now states explicitly that "the input box in this dialog is empty"; the question-setting flow now also says clearly whether the 『Question』 or the 『Answer』 is what is missing |
| 32 | When receiving a file there is no way to choose where to save it | The incoming-file prompt was only "yes/no", and the download folder was hard-coded in Settings | Added an incoming-file dialog: default folder / **Save as…** / Decline; `accept_file(save_path=…)` supports saving to any path (an existing target is renamed automatically) |
| 33 | After closing a dialog the console occasionally spits out `invalid command name "...<lambda>"` | Destroying a window makes Tkinter delete the callback command, while the `after` job scheduled earlier still fires | Added `winfocus.cancel_pending()`; every dialog now closes through `_close_toplevel()`, which cancels the pending job before destroying the window |
| 34 | **"I definitely typed the answer in, and it still says I didn't"** | In self-test mode one process contains **two `tk.Tk()`** instances; a `tk.StringVar()` without `master` binds to the **first** Tk interpreter. The input box in the second window used a variable of the same name from that interpreter, so the text was visible in the box while `answer_var.get()` always returned an empty string | All Tk variables now specify `master=` (their owning window) explicitly; the regression test now **really `insert`s into the input box and then submits** (the old test used `var.set()`, which happened to sidestep this trap) |
| 35 | When the other side is offline / you do not want to answer that question, there was no way out | There was no such action as "Cancel friend request" | Added the `friend-cancel` protocol message and `cancel_friend_request()`: both the answer dialog and the context menu can cancel with one click, and the question on the other side becomes void at the same time; a failed answer also asks whether to cancel |
| 36 | After the connection dropped, ✋ Answer question still held a question that could never be answered | The pending question was only overwritten by the next question and was not cleared when the connection closed | Clear the pending question for that peer when the session closes, and show "the other side's verification question has expired" |
| 37 | Once a question had been set it could not be cancelled, so the other side stayed shut out | Setting a question offered only "set", with no "undo" entry point (`clear_question` was reachable only from the question dialog in the context menu) | Added **"Cancel question"** to "Add contact", which also shows the question currently set for the selected person; the question dialog in the context menu keeps "Cancel question" |
| 38 | **The other side removed you / went offline, and the UI just showed "the person suddenly vanished", with no notice at all** | Such events emitted only an `ERROR`/`INFO` event, and the UI merely wrote it into the small line of text at the bottom; once the contact was removed even the chat window was gone | Added a **notification center** (`📢 Notifications (N)`, the latest 100 entries, double-click to jump to the corresponding person): being removed as a friend / being blocked gets **a dialog plus a record**, while cancelled requests and coming online or offline are only recorded without a dialog; offline friends are explicitly marked `[Offline]` in the list |
| 39 | The "change save location" entry point was too hard to find when receiving a file | There was only an unremarkable ghost button plus a read-only input box | The incoming-file dialog now has three equally prominent large buttons: **Save to default folder / Save to another location… / Decline**, with the full path of the default folder displayed right there; the transfer starts as soon as a location is chosen |
| 40 | **You wanted to pick a save location but no dialog appeared, and the file quietly landed in the default folder** | Self-test mode configured the instances with `auto_accept_files=True` (auto-receive), so no chooser dialog appeared at all and only a single line was written to the status bar | Self-test mode now does **not** auto-receive by default; when auto-receive is genuinely on, it also leaves an entry in `📢 Notifications` saying "auto-received xx → saved where", and shows a note in the chat area that it can be turned off in Settings |
| 41 | **Someone who had answered correctly could be removed/rejected and then added back without having to answer again** | The verification question hung off the **contact record**: removing a friend deleted the contact (together with the question and the answer hash), so by the time they were added back "this question" no longer existed | The question is now **stored separately by `peer_id`** (persisted in `settings.json`, decoupled from the friendship): removing a friend, rejecting, blocking and changing the question all clear the "they answered correctly" memory, while the question itself remains — adding them back requires answering again (regression tests cover removing a friend / rejecting / changing the question) |
| 42 | **"A wrong password gives no feedback at all", and the dialog closed as soon as you submitted it** | The dialog `close()`d immediately after the answer was sent; correctness had to wait for the other side's verdict, and the other side **never sent the verdict back**, so the user could only read the small text at the bottom | Added the `question-result` protocol message: both correct and wrong verdicts are returned. The dialog now **stays open after submitting**, showing "❌ Wrong answer, N attempts left" in place with a direct "Resubmit"; a correct answer shows "✅ Correct answer" and then closes the window automatically; with no response for 12 seconds it says "the other side may be offline". Re-applying for the same question **reuses the same nonce**, avoiding "I answered exactly what was on screen and it still came out wrong" |
| 43 | File transfer showed only a single small line, with no progress or speed | There was only one text label and no progress bar | Added a **progress bar** below the input box plus `percentage (sent/total size) · speed · time left`; when the transfer finishes it shows "completed + average speed" and then collapses automatically; it is hidden while idle |
| 44 | The phrase "save to the default location" was easy to misread, and a single unified download folder was wanted | The download folder was a default computed at startup, with nowhere for the user to change it, yet the dialog said "save to the default folder" | Added **"⚙ Settings → My download folder"** (the folder can be chosen and opened in one click, is stored in `settings.json`, and takes effect for existing sessions immediately); the dialog buttons became **"Accept (save to my download folder)" / "Save to another location…" / "Decline"**, dropping the wording "default folder" |
| 45 | Received files piled up in `~/lanchat_downloads` by default, and you had to go find them yourself | The default folder was hard-coded to a custom name | The default is now the system **Downloads folder** `~/Downloads` (with an automatic fallback when it is not writable), and Settings lets you change it to any folder |
| 46 | You could block someone from "Add contact" but not unblock them in the same view | The unblock entry point existed only in "Settings → Block list" and the context menu | "Add contact" gained an **"Unblock"** button: when the person is not blocked it says so clearly, and a successful unblock also pops up a confirmation |
| 47 | **What happens when the two sides have different key rotation settings?** | The old implementation had each side use its own value, and whoever initiated won — the other side could set 0 (off) and still be rotated, or set 30 itself and never rotate, so the two sides understood it differently | During the handshake each side puts its own `rekey` into hello for **negotiation** (`crypto.negotiate_rekey`: **if either side is 0 there is no rotation, otherwise the stricter (smaller) value wins**; if the other side is an old version the local value is used); changing the frequency in Settings sends `rekey-pref` to **tell the other side to renegotiate immediately**, with no reconnect needed; the UI shows "after negotiation this session: every N messages / no automatic rotation" |
| 48 | **Occasionally "ciphertext authentication failed" → disconnect → reconnect, looping over and over; the rotation counts on the two sides also failed to match** | Rotation has a window period: the other side switches to the new key **immediately** on receiving `begin`, whereas the initiator switches only after `accept`. Frames sent by the other side during that period already use the new key while the initiator accepts only the old one — failing to decrypt means (a) being silently dropped as a stale frame, or (b) being judged "ciphertext authentication failed" and triggering a disconnect and reconnect; one disconnect in turn makes both sides reset their rotation counters, which looks like "they are at 3 and I am at 2" | The initiator **derives the next round's key in advance** inside `rotate_keys()` (used only for decryption, not for sending), and on receiving a frame tries `current → next → previous (5-second grace)` in order; `tests/test_rekey.py::test_frames_in_rekey_window` pins down exactly this window (by holding back the acknowledgement) and asserts that `rekeys_done` must be equal on both sides within the same session |
| 49 | After upgrading, the default download folder was "still the old place" | Older versions wrote the **automatically computed** download folder into `settings.json` on startup too, and after the upgrade that "user setting" overrode the new default again | Only folders the **user actually chose** are recorded (a new `download_dir_chosen` flag), and the automatic paths used by older versions (`~/lanchat_downloads` and the like) are recognised and ignored outright; the default is always the system **Downloads folder** |
| 50 | In self-test mode received files ended up in the data directory `.lanchat\selftest\downloads` | Self-test mode's download folder was written as "under the data directory", inconsistent with normal mode | Self-test mode uses the system download folder as well (`~/Downloads/lanchat-自测/甲|乙`), just as easy to find as usual |
| 51 | **Blocking a friend and then unblocking them made the friend disappear outright; and the other side had no idea at all that you had unblocked them** | `unblock()` pushed the relationship back to `REQUEST_IN` ("someone wants to add me, pending my handling") across the board. So after unblocking, the person was gone from the friend list and lay in "New friends"; the other side kept showing "the other side blocked you", with no way to tell whether you had actually unblocked them | Remember whether they were a friend before the block (`friend_before_block`, persisted to disk); on unblocking, **a friend is still a friend** (a non-friend goes back to being a stranger, and a pending request is never fabricated), plus a new `friend-unblocked` protocol message that **notifies the other side proactively** (if there is no channel, remember it and resend when they next connect or apply again). Group 9 of `tests/test_integration.py` guards the whole chain |
| 52 | **After unblocking, when the other side adds you again the request already shows up under "New friends" before the question has been answered** | The same `REQUEST_IN` as above: the unblock itself "manufactured" a request for the other side that had never answered a question; on top of that, when the "answered correctly before" memory had not been cleared properly the request was let straight through | Unblocking always goes back to "stranger" and clears the answer memory; the other side must **answer the question again** when they reapply, and until they get it right they are invisible under "New friends" (the regression test asserts that `pending_requests()` is empty) |
| 53 | **A blocked user cannot tell "still blocked" apart from "the other side is not running"** | The blocked connection was closed silently during the handshake (`close()` sends an RST, which also wipes out the error frame that was just written), so the other side only saw "the other side disconnected early"; and on the blocked side the app wrote `blocked` (I blocked them) and `blocked_by_peer` (they blocked me) into the same place, so that side ended up rejecting the other side's connections in turn | The rejection now returns an explicit reason carrying `code="blocked"`, and `_reject()` (first `SHUT_WR`, then read until clean, then close) makes sure that frame really arrives; the two directions are recorded completely separately: when the other side blocks me only `blocked_by_peer` is written. The blocked side sees "still blocked", and receives a notification after being unblocked |
| 54 | Occasionally a friend request "does nothing when clicked" and only sorts itself out half a minute later | When both sides initiate a connection at the same time, the deduplication logic in `_handle_incoming` tears down one of them; if it happens to tear down the one that just sent the request, the request is lost, and the next redial waits 30 seconds | ① The session that was swapped out for the same peer is **kept as a spare reference** (`_spare_connections`), and when the primary drops it switches over immediately, so there is still a channel for sending and resending messages; ② added `friend-request-ack`: the other side replies as soon as it receives the request, and the requester resends if it hears nothing for 5 seconds (after at most 6 attempts it falls back to the routine 30-second redial); ③ receiving a duplicate friend request no longer downgrades someone who is already a friend into a "pending request" |
| 55 | **After unblocking, the other side never gets the notification and only finds out by applying all over again** | The "unblocked" notification was handled as "sent means delivered" (queuing the message into the queue ≠ the other side receiving it); and after unblocking the app did not dare dial out on its own (for fear of colliding with the other side dialing in at the same time), so if the other side did not connect first the notification never arrived | Notifications now **require an acknowledgement**: the other side replies `friend-unblocked-ack` upon receipt, and without an ack it stays pending; on unblocking it tries at once (send if there is a session, dial once if there is no channel), after which the connection manager retries every 5 seconds and delivers as soon as the other side comes online. A related bug was fixed at the same time: the unblock notification **no longer swallows the state of the request the other side just sent** (it used to change `request-out` to "stranger", which left both sides stuck) |
| 56 | "Accept friend request" gets lost too: the other side stays stuck on "waiting for the other side to accept" | `friend-accept` was likewise "sent means delivered"; if B happened to be offline or had its session torn down when A accepted, B never found out and the UI kept showing "waiting" | This was changed to require an acknowledgement as well (`friend-accept-ack`): `accept_pending` is persisted to disk and resent every time a session is established/restored, until the other side confirms; receiving a duplicate `friend-accept` also no longer pops up "new friend" a second time |
| 57 | **"It says the default download folder was changed, but it still uses the old path"** | `_load_settings()` treated `download_dir` in `settings.json` as a "user setting"; but **older versions wrote the path they computed automatically into it on every start**, so after an upgrade that "user setting" overrode the new default again (it took effect only after the user deleted `.lanchat` and let it be recreated) | The rule is now: **only a value carrying the `download_dir_chosen` flag is honoured** (that flag was added in the same version as "you can pick the folder yourself in Settings", so older versions cannot possibly have it), and anything else is ignored with the notice "Ignored the download folder written automatically by an older version: …"; the Settings window gained a one-click **"Use system download folder"** restore, so there is no need to go and delete `settings.json` any more (regression tests cover: an unrecognised old path must be ignored too) |
| 58 | **On networks where broadcast does not work (Tailscale / WireGuard / across subnets) you simply cannot add friends**, and yet the README said "you can enter IP:port manually" | That sentence was **false**: `register_manual_peer()` is only used internally by self-test mode, and "Add contact" had no entry point for typing an address at all. And even if it were implemented, adding an input box alone would not do — before the handshake a manual address **does not know the other side's real identity**, so the placeholder id does not line up with the `lc-xxx` from after the handshake (sending messages / saving / reconnecting are all mismatched), and the request signature is bound to the placeholder id as well, so the other side's signature check is bound to fail | **Manual mode** was added: `add_manual_peer()` plus an input box at the bottom of "Add contact" (enter `IP:port` to add and send a request in one step); during the handshake `_adopt_real_identity()` migrates the contact from `manual:IP:port` to the real identity (migrating the question I set for the other side along the way and cleaning up the placeholder entry); signatures now use the migrated real id; the manual address is persisted to disk in `manual_address` (this kind of network has no broadcast, so the discovery layer can never fill it in). A 10th integration scenario was added: **two instances that never tell each other their addresses, with discovery turned off**, getting through "request → accept → encrypted chat → still there after a restart" relying on manual add alone |
| 59 | **"Add manually asks for a port, but the port is random — so what am I supposed to enter?"** | The TCP chat port defaults to `0` = random on each start (irrelevant in broadcast mode, where the port is announced to the other side with the beacon); but manual mode requires the **added side** to pin its port, and previously only the command line `--port` could set it — so anyone who double-clicks the exe cannot use manual mode at all | The Settings window gained **"Local port"** (0 = random; 1024~65535, and it must not clash with the UDP discovery port), stored as `tcp_port` in `settings.json` and **effective after a restart**, with "Current actual port" shown in the UI as well; an explicit `--port` on the command line takes precedence over the setting. The hint text for manual add now spells out both approaches |
| 60 | **After a new item was added to the Settings window, the whole lower half (Local port / download folder / Close button) was cut off and could not be clicked at all** | `SettingsDialog` hard-coded `geometry("580x600")`, and there was no scrollbar even when the content was taller than the window | The settings content now lives in a **Canvas + scrollbar** (with mouse-wheel support), while the "Close" button at the bottom is pinned outside it; the window height is computed from the screen (everything fits at once on 1080p, and only small screens need scrolling). The regression test asserts that "the Close button is always inside the window" and that "Local port" is reachable by scrolling to the bottom |
| 61 | In manual mode the app kept redialing a **port that does not exist** | `_register()` stored the port from `sock.getpeername()` into `contact.address` as the other side's listening port; but on a connection where the other side **dials in**, that port is its ephemeral source port, not its listening port | This address is now recorded only when **we dial out ourselves** (only then is the peer's port its real listening port); a connection dialed in by the other side no longer stores this bogus address. In broadcast mode the address is refreshed by broadcast anyway, so nothing is affected |
| 62 | **Manual mode still forces you to pin a port, which is awkward** ("the TCP port is random anyway, so why don't we just ask?") | The old manual mode could only "connect straight over TCP to a known port", so the other side had to pin its port — yet the beacon already carries the other side's current TCP port, it is just that **broadcast cannot reach** them and nobody went and **asked over unicast** | **Unicast probing** was added: entering only an IP sends a UDP `k:"who"` to the other side's discovery port, and they **reply over unicast** with a beacon carrying their real TCP port (the reply carries `dport`, so it still lines up even when the two sides use different discovery ports) → neither side needs a pinned port; the probe port candidates are the locally configured one plus the standard `50505`. The old route of entering `IP:TCP port` is kept (for when the other side is invisible / has changed its port) |
| 63 | You want others to **not be able to find you**, while still using the app normally | The discovery layer only had on/off: turning it off meant you could not see others either, which was a poor experience | Settings gained **"Allow others to discover me"**: when it is off, the app **does not broadcast and does not answer unicast probes** (others can neither find nor probe you), but it **still receives broadcasts as before** (so I can see others and add them proactively), and chat / file transfer are completely unaffected; others can only add me by typing `my IP:TCP port` by hand. The switch is persisted to disk (`discoverable` in `settings.json`) |
| 64 | **"In self-test mode the invisibility switch does nothing when clicked"** | Self-test mode only let A turn on the discovery layer while B did not even receive: the "People on the LAN" list in both windows was **empty the whole time** (everything relied on the internal `register_manual_peer` registration), the discovery layer was not involved at all, so invisibility naturally showed no change whatsoever | In self-test mode **both instances turn on the discovery layer** (each one's own beacon is filtered out by peer_id, so there is no "finding yourself"): they now really see each other through broadcast; turn invisibility off → the other side disappears from the list within 12 seconds (offline timeout), and comes back when it is turned on again. In addition, "Probe" now only recognises people **learned by the discovery layer** (`_discovered_ids`), and no longer treats a locally registered address as a "successful probe" |
| 65 | Following on from the previous entry, the user pushed back: **"self-test forces the opposite side to see the opposite side, so how am I supposed to test hidden mode?"** — quite right: just letting both sides turn on the discovery layer is not enough | On self-test startup the app still unconditionally called `register_manual_peer()` to stuff `127.0.0.1:port` into "People on the LAN". So the list **always contained the other side**: you could not tell whether broadcast worked at all, and with invisibility off the opposite list still showed someone (that locally registered record has nothing to do with broadcast) | Self-test now **prefers real broadcast discovery**: after the two instances come up they wait for the discovery layer to learn about each other on its own (up to 9 seconds), and only fall back to registration when broadcast genuinely does not work, stating truthfully in the window that "broadcast does not work / invisibility cannot be tested". At the same time "locally registered addresses" and "people found by the discovery layer" are separated in the data layer (`DiscoveredPeer.manual`), the former is marked **manual entry** in the list, and `is_discovered()` only accepts the discovery layer — as soon as the discovery layer works, that mark disappears automatically. Two regression tests were added: "a request can be sent and an encrypted connection established through broadcast alone" and "the fallback path when broadcast does not work" |
| 66 | **Typing a Chinese colon by accident while adding manually adds the wrong person / throws an error** (under a Chinese IME `：` and `:` look almost identical) | The UI split the input on `:` and then called `int(port)` directly, and a Chinese colon does not split → so the whole string was taken as "only an IP was entered" and sent off for unicast probing (adding the wrong person), or it complained "Port is not a number: 50606" (with no way to see by eye what was wrong) | Input is now normalised before parsing (`normalize_address_input` / `split_manual_address`): Chinese colons, full-width digits / periods / spaces, a space instead of a colon, backticks, and an `http://` prefix plus path are all corrected automatically, and when a correction really happened the chat area states "Address cleaned up automatically: … → …"; input that should error out still errors out (port not a number / out of range / colon only). Unit tests cover 14 spellings, and a GUI smoke test verifies that Chinese-colon input really takes the "direct connection" path |
| 67 | **In the packaged exe, setting a nickname and clicking "Get started" first shows "Not responding" for a few seconds before the chat UI appears** (not noticeable when run from source) | `ChatService.start()` constructs `DiscoveryService` synchronously on the **Tk UI thread**, and its `__init__` has to enumerate the local network adapters — on Windows that means running an `ipconfig` subprocess. Measured: 0.04 seconds when run from source, **1.5~2.5 seconds once packaged as a windowed program** (spawning a subprocess is clearly slower without a console), and during those seconds the UI thread handles no messages → Windows marks it "Not responding". The startup log measured `Building main window… → Main window shown` = **6.55 seconds** | Network adapter enumeration was changed to **cached + done only on a background thread**: `interface_pairs()` caches for 30 seconds (concurrent calls run the enumeration only once), `warm_interfaces_async()` throws the enumeration onto a background thread as soon as the Tk window has been created (the time the user spends entering a nickname is enough for it to finish), and `DiscoveryService.__init__` only reads the cache and waits for the background thread to fill it in if it is missing (never blocking the caller); the discovery thread also re-enumerates every 120 seconds, incidentally fixing the hidden problem of "a VPN that connects only after startup leaves the broadcast target permanently stale". After the fix the same scenario takes **0.95 seconds** (`Service: building discovery layer` dropped from 2.48 seconds to 0.07 seconds), and in the packaged build the self-test's discovery via broadcast also improved from 4.7 seconds to 2.1 seconds. New regression: `test_interface_enumeration_is_cached_and_async` (with a cold cache, constructing the discovery layer must return immediately, the addresses are filled in from the background, and a cache hit does not re-enumerate) |
| 68 | **Chat content "is gone once you close it"**: after one side restarts, everything said before is empty (even **switching contacts** clears the chat area) | Chat content lives only in that `Text` widget in the UI: `select_contact()` calls `delete(1.0, end)` on every switch, and a process exit loses everything outright. The service side maintains only `last_text` (the preview line in the list) and has no notion of "this conversation", so after the other side restarts there is nothing to restore from | Added **in-session chat history** (`ChatService._chat_log`, the most recent 500 entries per person, **memory only, never written to disk**) + reconciliation and replay: once the channel is up and both sides are friends they send each other `history-state` (which entry of yours I received / which entry of mine I sent), and each side uses it to replay the missing ones in frames via `history-replay` (80 entries per frame); entries at or below a known sequence number are dropped outright, so neither reconnects nor repeated replays show duplicates. The UI keeps its own copy per person, so switching away from a contact and back leaves the content intact. Along the way, "sending a message while the other side is offline" changed from "blocked outright" to "recorded locally first and delivered when they come online", and on the other side such messages **count as new and unread** (a `pending` flag distinguishes them from "pure history"). Integration test group 11 was added (after B restarts it gets back A's 2 entries plus its own; the entry sent while offline is delivered as a new message; replaying does not break the session), unit tests cover reconciliation/deduplication/framing, and the GUI smoke test covers that switching contacts does not clear the chat area |
| 69 | When replaying history the **packets are especially large and easy to recognize** ("a man in the middle who sees a long burst of large packets knows history is being synced, and decrypting this one alone gets him all the earlier records"); there was also the worry "how can a rotated-out old key still decrypt history" | ① History replay is "one long stretch of plaintext stuffed into a few large frames", and the frame size is nothing like that of a normal chat message, so anyone capturing packets can classify it without decrypting; ② chat message lengths are exposed anyway (3 characters and half a screen of text differ greatly in length, and one can even guess how long a file name is). As for the "old key": **there is actually no such problem** — the in-memory record stores **plaintext**, and the replay is re-encrypted with the **current** key, so after rotation the old one is just as useless | Added **length obfuscation**: before encryption the plaintext is padded up to a multiple of 512 bytes, **then a random 0 to one whole step is added** (`PAD_RANDOM_EXTRA`; sending the same message 20 times in a row produces 18 different sizes), the padding comes from `os.urandom` and is authenticated by AEAD along with the rest; the decrypting side drops the padding field, unnoticed by the layers above. Also added **on-demand zlib compression** (`crypto.seal_message`: used only when it pays off; repeated content measured 8110 → 942 bytes); the order is "compress first, then pad" (the other way round the random padding bytes would ruin the compression ratio). History replay adds on top of that: **at most 80 entries per frame** plus a **random 50~250 milliseconds** between frames, sent on a background thread. Plaintext >16 KiB (file chunks) is neither padded nor compressed. New regression: `test_length_padding` (a random insertion every time, same-length content reveals nothing about its length, compression shrinks it and decompresses it byte for byte, large blocks are neither padded nor compressed) |
| 70 | **File transfer shares one key with chat**: a file of a few MB and dozens of chat lines run on the same key round | Session keys rotate by "count", so during a file transfer all the large blocks land in the same round; if that round is broken or recorded, the file contents and this stretch of chat are exposed together | **Rotate the key once before and once after the file transfer**: when the other side clicks "Accept", `request_rotation("file-start")`, and when the transfer is done (after the SHA256 check) once more with `request_rotation("file-end")`, retiring the round used for the file. Rotation is still performed only by the fixed "initiator" (if both sides initiated at the same time they would each use their own `fresh` and diverge straight away); the side that is not the initiator sends `rekey-request` asking the other side to rotate, with throttling + one replay retry (a small file may finish before the previous rotation has wrapped up). Group 5 of the integration tests gained assertions: the epoch increases by at least +2 across a 1.5 MB file transfer, the SHA256 still matches, and there is no "ciphertext authentication failed" |
| 71 | Self-test mode could only verify the "always online" flow, and the **disconnect → reconnect → replay** path, which is the most likely to go wrong, could not be verified by hand | Reproducing it required manually killing the process or unplugging the network cable, and the two instances of `--self-test` live in the same process, so the user cannot "make one of them disappear first" | The self-test window gained a **"🧪 Disconnect for 6 seconds"** button (`ChatService.simulate_offline`): it really drops all sessions, stops the discovery layer (sending bye, so the other side need not wait 12 seconds for the timeout) and closes the TCP listener; after 6 seconds it listens again on the **original port** and resumes broadcast; the other side's auto-reconnect brings it back. New regression: `test_simulate_offline_and_reconnect`: a message sent during the disconnect is recorded locally first (`pending`) → after coming back online it is still bound to the original port → both sides reconnect automatically → that message is delivered as a "new message" (unread +1) → normal chat still works afterwards |

| 72 | **Both sides have each other's conversation open, yet unread hints keep popping up** (the "(1)" just sits there and only disappears after another click) | Unread is incremented in the **service layer** (`contact.unread += 1`), while the UI calls `mark_read` only once, when a contact is opened/switched; while a conversation stays open, new messages are merely appended to the chat area and nobody ever clears the unread count — so it goes up by one for every incoming message, even though the message is already displayed right there | When the UI opens a conversation it tells the service who is currently open (new `set_active_peer`): messages received by **the person currently being viewed** no longer accumulate unread, and opening the conversation also resets the previous unread count to zero; it is cleared when switching away, closing the window, or when that person disappears from the list, and afterwards new messages count as unread as before. **Replayed/delivered offline messages** from the other side follow the same rules. New regression: integration test group 12 (no conversation open → unread +1; open it → reset to zero; a message while it is open → stays 0; switch away → +1; open again → back to zero) + a GUI smoke assertion (opening a conversation really tells the service about the current session, and switching away clears it) |

**Lessons learned** (now written into the code comments and tests):

1. **"The window was created" ≠ "the window is visible"**. You must check `winfo_viewable()` / the Win32 `IsWindowVisible`,
   and verify it with a screenshot or by enumerating windows; `tests/test_startup_window.py` guards this with a real subprocess launch.
2. **Tests must not exercise only one path**. Previously all GUI tests passed `--name`, which happened to bypass the only branch that had a bug.
   The startup tests now cover four paths: "without arguments / with arguments / --window-test / --self-test".
3. **Shared state needs a single writer**. Socket writes, nonce allocation and enqueueing must all be serialized, otherwise ordering is lost.
4. **Console encoding is a required subject for GUI programs on Chinese Windows**, especially when printing special characters.
5. **There must be explicit rules when the two ends' configurations differ**. For things that "both sides must agree on" such as the rotation frequency or the purpose declaration,
   either negotiate a definite value during the handshake or fix once and for all whose word decides — each side using its own is bound to cause problems (fixes #16, #47).

## 12. Project structure and tests

```
chat_gui.py             Entry point (environment self-check / startup log / UTF-8 / --diagnose / --self-test)
chatgui.py              Alias entry point with the same name (runs without typing the underscore)
使用说明.txt             Brief instructions distributed with the release package (copied next to the exe when packaging)
lanchat.spec            PyInstaller packaging configuration (defaults to a **folder build onedir**, no console window)
tools/i18n_extract.py   Extracts the UI strings from the source (used when adding a language)
tools/i18n_check.py     Checks whether each language catalog is complete
docs/开发文档.md          This file
docs/Development.md     The English version of this file
docs/screenshot-*.png   UI screenshots
lanchat/
    console.py          Console encoding (GBK → UTF-8, avoids crashes when printing special characters)
    startup_log.py      Step-by-step startup log with timestamps
    winfocus.py         Really brings the window to the foreground (bypasses Windows focus-stealing prevention)
    constants.py        Protocol constants (port, timeouts, chunk size, data directory…)
    protocol.py         NDJSON frame send/receive (resistant to sticky packets/partial packets/oversized frames)
    identity.py         Long-term identity key (Ed25519/X25519), identity card, fingerprint, data directory
    crypto.py           HTTPS-style handshake: X25519 ECDH + Ed25519 signature + HKDF + AES-256-GCM
    discovery.py        UDP broadcast auto-discovery (cross-platform network adapter/directed broadcast address detection)
    connection.py       A single encrypted session (single writer thread guarantees ciphertext ordering + file chunk stream)
    service.py          Overall control: discovery + friend relationships/block list + multiple sessions + reconnect + file transfer
    gui.py              UI: set nickname / add contact / new friends / chat / settings / self-test mode
    i18n.py             Multilingual: t() lookup, language normalization, system language detection, per-language font selection
    locales/en.py       English strings (CATALOG = {Chinese source: English})
    locales/zh_hant.py  Traditional Chinese (Taiwan usage) strings
tests/
    test_units.py           Units: identity card/handshake/tamper and replay protection/friend request signature/rotation derivation/verification question/framing
    test_rekey.py           Session-layer key rotation: rotation keeps happening + epoch matches under two-way cross fire + SHA256 verified for a 2MB file during frequent rotation
    test_integration.py     Integration: add friend/decline/block and unblock/group chat/file/packet capture/reconnect/persistence (8 groups)
    test_gui_smoke.py       GUI smoke: real window + real encrypted connection, driving the button logic
    test_startup_window.py  Startup regression: real subprocess launch, covering the without arguments/with arguments/--window-test/--self-test paths (guards "window visible" and "self-test mode port valid")
    test_self_test_mode.py  Self-test mode: two windows + answer feedback (real keyboard input/wrong answer retry) + verification question (re-asked after cancel/restart/remove friend) + notification center + save-as for received files/unified download folder + unblock button + rotation negotiation + block/file/packet capture
    test_i18n.py            Multilingual: catalog completeness + t() behavior + language priority + persistence + real windows built in all three languages
```

**The following exist only locally and are not in the repository** (blocked by `.gitignore`; `docs/Development.md` has the same note):

```
dist/          Build output (dist/LanChat/LanChat.exe and the zip), not committed to the repository
build/         PyInstaller intermediate output
packaging_env/ Locally created packaging virtual environment (see "Repackaging" at the beginning of this document), not committed to the repository
tests/         Test scripts, not published with the public repository (see below)
```

> Note: the `tests/` test scripts in the listing above are kept only in the local development environment and are not published with the public repository. The commands below are for local self-testing.

```bash
python tests/test_units.py              # Unit tests
python tests/test_rekey.py              # Key rotation (zero-loss verification)
python tests/test_integration.py        # Multi-instance integration (several instances started on this machine at once)
python tests/test_integration.py --only 1,3,5
python tests/test_gui_smoke.py          # GUI smoke
python tests/test_startup_window.py     # Startup window regression (real process + Win32 window enumeration)
python tests/test_self_test_mode.py     # Self-test mode automation
```

The integration tests **really** start several instances on this machine at the same time and really go through TCP and the encrypted handshake: add friend → accept/decline/block/unblock
→ three people chatting with each other in encrypted form → the SHA256 of a 1.5 MB file matches → the bytes captured off the wire are pulled out and searched, confirming that nothing can be found
of the plaintext messages, plaintext file names or plaintext data chunks.

Group 9 covers **block/unblock semantics** (`--only 9`): block a friend → after unblocking you are **still friends** (it is not removing a friend,
and no extra "New friends" request that never answered a question appears out of thin air) → the other side receives an "Unblocked" notification and the encrypted session is restored automatically;
the blocked side is told "you are still blocked" when it applies again; after unblocking, applying again **still requires answering the verification question correctly first**.

Group 10 covers **manual mode** (`--only 10`): the two instances **never register each other's address and cannot see each other at the discovery layer either**
(different discovery ports simulate "broadcast does not work"), covering three approaches —
① enter only the IP → a **unicast probe** obtains the other side's TCP port automatically → add the friend directly;
② when the other side is **invisible** the probe finds nothing (an explicit failure) → switching to a direct `IP:TCP port` connection still works;
③ pin the **local port** in Settings → after a restart it really binds to that port and the other side can add you by it.
It also asserts that "contacts and addresses are still there after a restart".

---

## 13. Internationalization (i18n)

Interface language: **Simplified Chinese (source language) / Traditional Chinese (Taiwan wording) / English**.

### 13.1 Why use "the Chinese source text as the key"

`t()` in `lanchat/i18n.py` takes the **Chinese source text** directly as the lookup key, instead of a symbolic key such as `gui.send_button`:

* Chinese is the source language, so **a missing entry returns the Chinese text unchanged** — no missed translation can ever make `gui.send_button` show up in the interface; the worst case is just "this part is not translated yet";
* `t("发送")` in the code makes it obvious at a glance what that line displays, with no jumping back and forth between files to look up a key table;
* The cost: editing the Chinese source text is the same as editing the key, and the old translation becomes void (it falls back to Chinese). So **when you change the wording, change the keys in
  `lanchat/locales/*.py` at the same time**; `tests/test_i18n.py` lists every entry that "exists in the code but not in the catalog".

### 13.2 How to write it in code

```python
from .i18n import t

label = tk.Label(frame, text=t("发送"))                    # static text
msg = t("已向 {0} 发出加好友请求, 等待对方确认…").format(name)   # with parameters: t() itself does no formatting
```

**`t()` does no formatting** (it takes no `**kwargs`), and interpolation is always written as `t("…{0}…").format(...)`. That way the braces that
appear in help text (JSON examples and the like) do not accidentally trigger formatting, and therefore cannot raise an exception.

### 13.3 Where the language comes from

Priority: `--lang` > `language` in `settings.json` > system language (the `LANCHAT_LANG` environment variable can override it,
for tests). `chat_gui.py` fixes the language **before** `check_environment()` — otherwise startup failure messages such as
"cryptography is missing" would be in Chinese, exactly when English-speaking users most need to understand them.

**Switching takes effect only after a restart**: the text in the interface is written in once when the widgets are built, and changing the language at runtime would require rebuilding the whole widget tree
(popups, menus, and already-open dialogs would all have to be rebuilt along with it), which brings more risk of breakage than benefit. In the settings window the choice is only written to disk and takes effect on the next start.

Fonts are picked per language (`i18n.LANGUAGES[code]["fonts"]`): Simplified `Microsoft YaHei UI`,
Traditional `Microsoft JhengHei UI`, English `Segoe UI` — on Windows, Tk does not fall back between fonts automatically,
and rendering Chinese with an English font drops glyphs.

### 13.4 The message catalog and packaging

* `lanchat/locales/en.py` and `lanchat/locales/zh_hant.py`, each exporting `CATALOG = {Chinese: translation}`.
* `i18n.CATALOG_MODULES` is a **hard-coded table** (not a dynamically imported module name built by concatenating strings) — PyInstaller's static analysis cannot see
  a module name that is put together at runtime, and the packaged build then raises `ModuleNotFoundError`; it must also be listed in `hiddenimports` of `lanchat.spec`.
* A failure to load the catalog does not stop the program from starting: the exception is caught and it falls back to Chinese (setting `LANCHAT_I18N_DEBUG=1` prints the reason).

### 13.5 Tests

```bash
python tests/test_i18n.py      # catalog completeness + t() behavior + language priority + persistence + building real windows in all three languages
```

The "catalog completeness" check is the key one: it uses AST to scan out every `t("…")` in the source, checks entry by entry whether the en / zh-Hant catalogs have a corresponding
translation, and verifies that the English contains no Chinese characters and that placeholders correspond one-to-one with the original; "building real windows" builds the main interface once each with en / zh-Hant,
asserting that there is no whole piece of text that "should have been translated but is still Chinese".

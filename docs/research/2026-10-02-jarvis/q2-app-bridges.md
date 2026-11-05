# Q2: Apple app bridges for Claude Code + MCP (voice assistant)
Date: 2026-10-02. Method: WebSearch/WebFetch only (sandbox blocked `gh api` TLS and `osascript`, so nothing was run locally). GitHub fields below come from fetched repo pages / commit atom feeds; "last commit" given only where an atom feed was fetched.

## 1. `shortcuts` CLI (bridge type 1)
- Syntax (Apple docs): `shortcuts run "<name>" [-i|--input-path <files...>] [-o|--output-path <file>] [--output-type <UTI>]`; `shortcuts list [-f <folder>] [--folders]`; `shortcuts view <name>`. Exit 0 ok / 1 error.
- Data in: only via `-i` file paths; piped data is treated as text ("a path passed by pipe is treated as text", so use `-i` for files). Data out: `-o` file (extension/UTI decides type). Practical pattern: write JSON/text to a temp file, `-i` it, read `-o` file.
- Reach: anything with a Shortcuts action: Calendar/Reminders/Notes/Mail/Messages/Music/Home/Set Focus, plus 3rd-party App Intents. Key strength: the user pre-authors each shortcut, so the model can only call a fixed allowlist (named verbs), not arbitrary scripts. Best least-privilege bridge.
- Limits: parameters are not typed args (one input blob; the shortcut must parse it); outputs are one item; shortcuts that need UI/confirmation ("Ask Each Time", Send Message "Show When Run") can block or time out headless; shortcut names must match exactly.
- Permissions: first run of a shortcut touching an app/data raises per-shortcut "Allow <shortcut> to access ..." prompts (stored per shortcut, in Shortcuts, not TCC-by-terminal). Some data-type prompts remain; run each shortcut once manually in the Shortcuts app to pre-grant. [exact prompt behaviour for CLI-run not verified here]

## 2. osascript / JXA + TCC (bridge type 2)
- Scriptable with dictionaries: Calendar, Reminders, Notes, Mail, Messages (send/chats; individual messages NOT exposed, apple-bridge README confirms), Contacts, Music, Finder, Safari, System Events (UI scripting needs Accessibility). Home/HomeKit has no AppleScript; Focus has no AppleScript API (use Shortcuts).
- TCC Automation: each controlled app needs a (client -> target app) grant. The prompt names the *responsible process*, i.e. the GUI app that launched the chain (Terminal/iTerm/Claude desktop host), not osascript. Sources: Michael Tsai, steipete, anthropics/claude-code#98783. Child processes inherit the responsible process, so for Ed the grant lands on whichever terminal/app hosts the Claude Code session (and the eridian daemon if it is a separate launchd job, it needs its own grants).
- Error -1743 = not authorized. Grants live in System Settings > Privacy & Security > Automation; reset with `tccutil reset AppleEvents`. Reading `chat.db`/Mail/Notes SQLite needs Full Disk Access for the same responsible process.
- Claude Code specifics: an issue found (rjwalters/loom#6367) says spawned Claude sessions may get a clean tool denial for osascript instead of a TCC prompt; Claude Code's own permission rules can gate `osascript` Bash calls. [Claude Code behaviour not tested locally]
- Risk: osascript is arbitrary code (do shell script, keystroke). Prefer wrapping in an MCP server with fixed verbs.

## 3. Existing MCP servers (all fetched; all say local / no cloud unless noted)
| Repo | License | Last commit / status | Mechanism | Notes |
|---|---|---|---|---|
| github.com/supermemoryai/apple-mcp | MIT | ARCHIVED 2026-01-01; 3.1k stars | AppleScript | Messages send/read, Notes, Contacts, Mail send/search, Reminders, Calendar, Maps. Original "apple-mcp"; do not adopt. |
| github.com/boutquin/apple-bridge | MIT | 2026-09-24 (v3.2.0); 4 stars | Swift 6; EventKit (Cal/Reminders), AppleScript (Contacts/Notes/Mail/Messages send), SQLite chat.db (Messages read), MapKit | 37 tools, 7 domains. No send confirmation. Needs FDA for Messages read. |
| github.com/JonathanRReed/Apple-MCPs | MIT | 2026-09-30; 19 stars | unified + per-app servers, stdio or localhost HTTP | Read-only mode per app, allowed-roots for files/attachments. Includes Shortcuts, Files, System exec (high-risk, disable). |
| github.com/krmj22/macos-mcp | MIT | 2026-08-20; 9 stars | EventKit Swift binary (Cal/Reminders), JXA (Notes/Contacts/writes), SQLite reads (Mail/Messages) | 198 commits. |
| github.com/captainmark23/mac-apps-mcp | MIT | no date fetched; 3 stars | SQLite reads + JXA writes (Mail/Cal/Reminders/Contacts) | Best safety knobs: MACOS_MCP_READONLY, CONFIRM_DESTRUCTIVE, write rate limit (10/min), serialized write queue. |
| github.com/Jameshuff91/apple-mcp | MIT | no date; 0 stars (new) | Python; SQLite ro (chat.db, NoteStore, AddressBook); AppleScript/GUI writes | Read-only default; send/writes/shortcuts opt-in env flags; recipient limited to contacts; untrusted-content tagging. Needs FDA. |
| github.com/PsychQuant/che-ical-mcp | MIT | v1.18.0; 38 stars; no commit date | Swift EventKit, 29 tools | Calendar+Reminders only; Calendar/Reminders TCC; undo/redo; macOS 14+. |
| github.com/l22-io/orchard-mcp | MIT | no date; 21 stars | TS server + Swift CLI (EventKit, ScriptingBridge) | Calendar, Mail (search/read, draft only), Reminders, Contacts, iWork. States no network. Draft-not-send is a good safety shape. |
| github.com/Puhavik/apple-calendar-mcp, eneko-codes/apple-reminders-mcp, farmerajf/apple-events-mcp, EliasWiegandt/mac-app-integration | not fetched | not fetched | EventKit (EliasWiegandt: read-only iCloud Mail) | Exist per search results only. |
| github.com/tarun101/imessage-mcp | (fetch 404; search snippet only) | unverified | chat.db read + send with opt-in gating, recipient allowlist, audit log | Best-designed iMessage design per description; unconfirmed. |
| github.com/daveremy/imessage-mcp | MIT | no date; 2 stars | chat.db SQLite read, JXA send to existing chats only | "always confirm before sending" in tool description (soft gate only). |
| github.com/anipotts/imessage-mcp, wyattjoh/imessage-mcp | not fetched | not fetched | read-only chat.db | Exist per search. |
| github.com/epheterson/mcp-applemusic | MIT | no date; 105 stars; 137 commits | AppleScript (native engine) + optional Apple Music API / Safari / Chrome engines | Pure-local only in native engine; API/Safari engines use cloud. |
| github.com/pedrocid/music-mcp | MIT | no date; 7 stars | AppleScript, npm `@pedrocid/music-mcp` | Playback, search, playlists, queue. Simplest local-only. |
| github.com/omarshahine/HomeClaw | MIT | no date; 174 stars | Mac Catalyst app, HMHomeManager, loopback HTTP MCP 127.0.0.1:9090 + stdio | Read tools default, writes opt-in. Needs `com.apple.developer.homekit` entitlement => dev-signed with paid Apple Developer account, UDID-registered, macOS 26+. Heavy setup. |
| somethingwithproof/shortcuts-mcp, thomasvincent/home-mcp, coalsi/HomeMCPBridge, grahamaloo/HomeKitMCP | not fetched (shortcuts-mcp URL 404'd on fetch; home-mcp URL in search differs from owner name) | unverified | Shortcuts-based / HomeKit | Appear in search only; treat as unconfirmed. |
| Notes | no dedicated server verified | | covered by the multi-app servers (AppleScript/JXA writes, NoteStore.sqlite reads) | |

## Table: app | best bridge | candidate MCP | read | write | injection exposure
| App | Best bridge | Candidate MCP (license, maint.) | Read actions | Write / side-effect | Injection exposure |
|---|---|---|---|---|---|
| Calendar | EventKit (MCP) or Shortcuts | che-ical-mcp (MIT, active); apple-bridge (MIT, 2026-09) | list/search events, conflicts | create/update/delete event (invites to attendees = outward side effect) | Medium: event titles/notes/invite text from others |
| Reminders | EventKit | che-ical-mcp; apple-reminders-mcp [unfetched] | list/search | create/complete/delete | Low (self-authored mostly; shared lists add some) |
| Notes | AppleScript/JXA or SQLite read | Jameshuff91/apple-mcp (read-only default); apple-bridge | list/read | create/edit | Low-medium (web clippings, shared notes) |
| Mail | AppleScript read; draft-only write | orchard-mcp (draft only); mac-apps-mcp (has `mail_send`, rate limited) | search/read bodies | send (irreversible, exfil channel) | HIGH: arbitrary third-party bodies |
| Messages | SQLite read (FDA); AppleScript/JXA send | Jameshuff91/apple-mcp (injection tagging, gated send); tarun101/imessage-mcp [unconfirmed] | read chats | send iMessage/SMS (irreversible, to real people) | HIGH: attacker-controlled text from anyone who can text Ed |
| Contacts | JXA / Contacts framework | apple-bridge; krmj22/macos-mcp | lookup | edit/create | Low |
| Music | AppleScript (or Shortcuts) | pedrocid/music-mcp (MIT, small); epheterson/mcp-applemusic (MIT, 105 stars) | now playing, search | play/pause/volume/playlists | Low (track metadata only) |
| Home/HomeKit | Shortcuts (pre-authored scenes) or HomeClaw | HomeClaw (MIT; entitlement burden) | status/state | set devices, locks/garage = physical security side effects | Low in, but high consequence out |
| Focus | Shortcuts only (no AppleScript/MCP found) | none verified | none | set Focus | None |

## Recommended starter set (3)
1. **Reminders + Calendar read/add via EventKit MCP (che-ical-mcp, MIT).** High voice value ("what's next", "remind me ..."), no Automation prompts (EventKit TCC only), mostly self-authored data, undo exists. Start with write limited to reminders; calendar create without attendees.
2. **Music via `shortcuts run` wrappers or pedrocid/music-mcp.** Pure local, no sensitive data in, no third-party text; playback is the most natural voice command. Shortcuts wrappers give a fixed allowlist with no new server code.
3. **Home scenes via pre-authored Shortcuts (`shortcuts run "Good Night"`)** instead of HomeClaw (entitlement/Developer account overhead). Allowlist scenes only; keep locks/garage out.
Defer Mail and Messages: high injection exposure and irreversible sends. If added later, read-only first (or draft-only like orchard-mcp) and require the voice confirmation gate already built in eridian for sends.

## Cross-cutting notes
- Many "MCP servers" are one-person repos with <30 stars; vet code before granting FDA. FDA + Automation grants go to the responsible process (the terminal hosting Claude Code), widening that app's reach for everything it launches.
- "Local-only" means no relay; the returned text still goes to Anthropic as model context (JonathanRReed README says the same).
- Prefer servers with read-only default + opt-in writes (Jameshuff91, mac-apps-mcp, Apple-MCPs).

## Sources (fetched this session unless marked)
- https://github.com/supermemoryai/apple-mcp
- https://github.com/boutquin/apple-bridge (+ /commits/main.atom)
- https://github.com/JonathanRReed/Apple-MCPs (+ atom)
- https://github.com/krmj22/macos-mcp (+ atom)
- https://github.com/captainmark23/mac-apps-mcp
- https://github.com/Jameshuff91/apple-mcp
- https://github.com/daveremy/imessage-mcp
- https://github.com/PsychQuant/che-ical-mcp
- https://github.com/l22-io/orchard-mcp
- https://github.com/omarshahine/HomeClaw
- https://github.com/epheterson/mcp-applemusic
- https://github.com/pedrocid/music-mcp
- https://support.apple.com/guide/shortcuts-mac/run-shortcuts-from-the-command-line-apd455c82f02/mac
- https://steipete.me/posts/2025/applescript-cli-macos-complete-guide
- https://mjtsai.com/blog/2025/07/07/the-curious-case-of-the-responsible-process/ [search snippet only]
- https://github.com/anthropics/claude-code/issues/98783 [search snippet only]
- https://github.com/rjwalters/loom/pull/6367 [search snippet only]
- https://github.com/tarun101/imessage-mcp [search snippet only; fetch 404]
- https://github.com/somethingwithproof/shortcuts-mcp [fetch 404; unverified]

## Not verified
- Last-commit dates for all repos except apple-bridge, Apple-MCPs, macos-mcp; maintenance of the rest unknown.
- tarun101/imessage-mcp, somethingwithproof/shortcuts-mcp, thomasvincent/home-mcp, coalsi/HomeMCPBridge, grahamaloo/HomeKitMCP, Puhavik/eneko-codes/farmerajf/EliasWiegandt, anipotts/wyattjoh imessage servers: fetched not or 404; existence/licences from search listing only.
- Whether `shortcuts run` accepts stdin text and prints output to stdout (docs imply pipe-as-text; unrun).
- Per-shortcut permission prompt behaviour when run headless from CLI; Focus-setting action headless.
- Whether Claude Code's sandbox/permissions block osascript prompts for Ed's setup (osascript is blocked in this research sandbox; not tested on Ed's host).
- Which process (terminal vs eridian daemon) will hold TCC grants in Ed's deployment.
- EventKit servers' behaviour on macOS 26 (Tahoe) beyond che-ical-mcp's notarization claim.
- Any Notes-specific or Focus-specific MCP server.

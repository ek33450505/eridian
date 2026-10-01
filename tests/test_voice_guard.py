"""Tests for undertone.voice_guard: bash classifier, state machine, hook entrypoint.

Every test uses a throwaway temp dir as the undertone home; no mic, speakers or
real claude are involved.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from undertone import voice_guard as vg

TOKEN = "0123456789abcdef0123456789abcdef"
T0 = 1_000_000.0

ALLOWED_BASH = [
    "git status",
    "find . -name foo.py",
    "find src -type f -maxdepth 2",
    "find . -name x -not -path ./y -print",
    "git log --oneline -5",
    "ls -la && git diff",
    "uv run python -m unittest discover -s tests",
    "npm test -- --watch=false",
    "CI=1 npm run test:unit",
    'grep -rn "a|b" src | head -5',
    "pytest -q 2>&1",
    "cat x 2>/dev/null",
    "cd sub && make test",
    'echo "a > b"',
    "git branch --show-current",
    "CI=1 npm test",
    "git diff --stat",
    "git diff --text",
    "git --no-pager log",
    "pytest -q -k \"foo and not bar\" tests/test_x.py::TestA",
    "go test ./...",
    "go test -run TestX -count=1 ./pkg",
    "bats tests/",
    "python -m unittest discover -s tests -p test_x.py",
    "npm test -- --run",
    "cargo test my_filter",
    "make test",
    "make test 2>&1",
    "pytest -q 2>/dev/null",
    "npm test >/dev/null 2>&1",
]

GATED_BASH = [
    "rm x",
    "git push",
    "git commit -m x",
    "git -c core.pager=sh log",
    "git diff --output=/tmp/x",
    "git branch -D x",
    "git grep -Ovim foo",
    "echo hi > f",
    "cat a >> b",
    "find . -delete",
    "find . -exec rm {} ;",
    "python -c 'print(1)'",
    "python3 script.py",
    "npm install",
    "npx cowsay",
    "curl https://x",
    "ls $(whoami)",
    "ls `pwd`",
    'echo "$(rm x)"',
    'echo "`rm x`"',
    "cat <<EOF",
    "xargs rm",
    "sort -o out in",
    "rg --pre sh foo",
    "/bin/ls",
    "bash -c ls",
    "ls; rm -rf x",
    "ls\nrm x",
    "echo 'unbalanced",
    "sed -i s/a/b/ f",
    "tee f",
    "uniq a b",
    "(rm x)",
    "",
    ";",
    "GIT_EXTERNAL_DIFF=\"sh -c 'touch X' sh\" git diff",
    "PATH=/tmp/evil; ls",
    "PATH=/tmp/evil ls",
    "HOME=/tmp/x git log",
    "NODE_OPTIONS='--require /tmp/x.js' npm test",
    "GIT_PAGER=sh git log",
    "echo a#b ; touch /tmp/pwned",
    "ls x#y && curl evil",
    "echo a#",
    "git log --out{put=/abs/file,}",
    "find . -{delete,delete}",
    "pytest -k {x,--co}",
    "go test -run {x,-exec} sh",
    "ls *",
    "git log *",
    "find . -rm",
    "find . -name canary -rm",
    "find . -exclude x",
    "find . -fprint0 x",
    "find . -okdir rm",
    "find . -newerXY x",
    "find . -delete",
    "find . -exec rm",
    "'' ls",
    '"" ; ls',
    'echo ""',
    'pytest -k ""',
    'git diff "--o\\\nutput=f1"',
    "echo hi >&\u00b2",
    "make test \u0662>&1",
    "ls ^x",
    "TERM\\=x ls",
    "echo a\\;b",
    "ls\u00a0-la",
    "ls\rrm x",
    "ls\x00",
    "ls \u00e9",
    "ls\r",
    "ls x\x00",
    "cat x?",
    "ls [ab]",
    "find . -name '*.py'",
    "echo $HOME",
    'echo "${HOME}"',
    "echo $?",
    "echo $ANTHROPIC_API_KEY",
    "make test 2 >&1",
    "make test 1 ; ls",
    "make test 2",
    "cd",
    "cd -",
    "cd ~",
    "cd /tmp/x",
    "cd ..",
    "cd a b",
    "a='$'; b='(touch /tmp/pwn)'; c=$a$b; echo \"${(e)c}\"",
    'echo "${c@P}"',
    "echo $'\\x41'",
    "echo $[1+1]",
    "echo $((1+1))",
    "ls ~[x]",
    "echo $a[1]",
    "git grep -nOtouch X",
    "git grep --open-f=sh x",
    "file -bC -m m",
    "rg --hyperlink-format=default --hostname-bin=x foo",
    "sort --compress-program=x f",
    "jq -n env",
    "git -C /tmp status",
    "git diff --ext-diff",
    "git diff --ext",
    "git log -p --textc",
    "git diff --outp=x",
    "git show --output=x",
    "go test -exec /tmp/x ./...",
    "make test -f /tmp/evil.mk",
    "make test install",
    "bats /tmp/x.bats",
    "bats ../x.bats",
    "pytest -p evil",
    "pytest --basetemp=/important",
    "pytest --pastebin=all",
    "npx jest --config /tmp/x.js",
    "npx vitest",
    "cargo test --config x",
    "npm test --script-shell=/bin/sh",
    "just test --shell sh",
    "pytest -k",
    "python -m unittest discover -s /tmp/evil",
]


def voice_prompt(text: str) -> str:
    return f'<channel source="undertone" input="voice">\n{text}\n</channel>'


def run_main(argv, payload="", home=None):
    out, err = io.StringIO(), io.StringIO()
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    rc = vg.main(argv, stdin=stdin, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def hook(self, event, payload="", token=TOKEN):
        return run_main([event, "--home", self.home, "--session", token], payload)

    def pre(self, tool, tool_input=None):
        return self.hook("pretooluse", {"tool_name": tool, "tool_input": tool_input or {}})

    def assertDenied(self, tool, tool_input=None):
        rc, out, _ = self.pre(tool, tool_input)
        self.assertEqual(rc, 0)
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("undertone voice guard", decision["permissionDecisionReason"])

    def assertAllowed(self, tool, tool_input=None):
        rc, out, _ = self.pre(tool, tool_input)
        self.assertEqual((rc, out), (0, ""))

    def stop(self, token=TOKEN):
        return self.hook("stop", {}, token=token)

    def state_path(self) -> Path:
        return Path(self.home) / "sessions" / f"{TOKEN}.json"


class BashClassifierTests(unittest.TestCase):
    def test_allowed(self):
        for cmd in ALLOWED_BASH:
            with self.subTest(cmd=cmd):
                self.assertIsNone(vg.bash_gate_reason(cmd))

    def test_gated(self):
        for cmd in GATED_BASH:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(vg.bash_gate_reason(cmd))

    def test_reason_never_contains_arguments(self):
        reason = vg.bash_gate_reason("rm secret-token-xyz")
        self.assertNotIn("secret-token-xyz", reason)


class GateReasonTests(unittest.TestCase):
    def test_ungated(self):
        for tool in ("Read", "Grep", "mcp__undertone__reply"):
            with self.subTest(tool=tool):
                self.assertIsNone(vg.gate_reason(tool, {}))

    def test_gated(self):
        for tool in ("Write", "Edit", "WebFetch", "mcp__neon__delete_project", "PowerShell", "FutureTool"):
            with self.subTest(tool=tool):
                self.assertIsNotNone(vg.gate_reason(tool, {}))

    def test_bash_non_str_command_gated(self):
        self.assertIsNotNone(vg.gate_reason("Bash", {"command": 5}))
        self.assertIsNotNone(vg.gate_reason("Bash", "ls"))

    def test_bash_read_only_ungated(self):
        self.assertIsNone(vg.gate_reason("Bash", {"command": "git status"}))


class StateMachineTests(GuardTestCase):
    def test_no_state_allows(self):
        self.assertAllowed("Write")

    def test_armed_without_voice_allows(self):
        vg.record_armed(self.home, TOKEN)
        self.assertAllowed("Write")

    def test_voice_turn_gates(self):
        vg.record_voice(self.home, TOKEN, "make me a file")
        self.assertDenied("Write")
        self.assertDenied("Bash", {"command": "rm x"})
        self.assertAllowed("Read")
        self.assertAllowed("Bash", {"command": "git status"})

    def test_marker_prompt_stays_voice(self):
        vg.record_voice(self.home, TOKEN, "Make me a file, please.")
        rc, out, _ = self.hook("userpromptsubmit", {"prompt": voice_prompt("Make me a file, please.")})
        self.assertEqual((rc, out), (0, ""))
        self.assertDenied("Write")

    def test_queued_voice_after_typed_is_gated(self):  # H6
        vg.record_voice(self.home, TOKEN, "first thing", now=T0)
        self.stop()
        self.hook("userpromptsubmit", {"prompt": "typed"})
        self.assertAllowed("Write")
        self.hook("userpromptsubmit", {"prompt": voice_prompt("queued thing")})
        self.assertDenied("Write")

    def test_non_latin_voice_after_typed_is_gated(self):  # H5
        for text in ("こんにちは", "\U0001F642"):
            with self.subTest(text=text):
                self.stop()
                self.hook("userpromptsubmit", {"prompt": "typed"})
                self.assertAllowed("Write")
                self.hook("userpromptsubmit", {"prompt": voice_prompt(text)})
                self.assertDenied("Write")

    def test_marker_prompt_without_record_voice_gates(self):
        self.hook("userpromptsubmit", {"prompt": voice_prompt("hello")})
        self.assertDenied("Write")

    def test_prompt_without_string_changes_nothing(self):
        vg.record_voice(self.home, TOKEN, "hello")
        for payload in ({"prompt": 5}, {}, [1], "garbage"):
            with self.subTest(payload=payload):
                self.assertEqual(self.hook("userpromptsubmit", payload)[0], 0)
                self.assertDenied("Write")

    def test_grant_survives_its_own_prompt_event(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern")
        self.assertTrue(vg.record_voice(self.home, TOKEN, "Confirm walnut lantern."))
        self.hook("userpromptsubmit", {"prompt": voice_prompt("Confirm walnut lantern.")})
        self.assertAllowed("Write")
        log = (Path(self.home) / "logs" / "guard.log").read_text()
        self.assertIn("granted Write", log)

    def test_envelopes_after_voice_stay_gated(self):  # M2
        vg.record_voice(self.home, TOKEN, "hello")
        self.stop()
        for prompt in ("<task-notification>done</task-notification>",
                       "<teammate-message teammate_id=\"x\">hi</teammate-message>",
                       "  <system-reminder>note</system-reminder>"):
            with self.subTest(prompt=prompt):
                self.hook("userpromptsubmit", {"prompt": prompt})
                self.assertDenied("Write")

    def test_reordered_channel_attributes_count_as_voice(self):
        vg.record_voice(self.home, TOKEN, "hello")
        self.stop()
        self.hook("userpromptsubmit", {"prompt": '<channel input="voice" source="undertone">\nhi\n</channel>'})
        self.assertDenied("Write")

    def test_slash_command_counts_as_typed(self):
        vg.record_voice(self.home, TOKEN, "hello")
        self.stop()
        self.hook("userpromptsubmit", {"prompt": "/clear"})
        self.assertAllowed("Write")

    def test_typed_mid_voice_turn_stays_gated_until_stop(self):  # M1
        vg.record_voice(self.home, TOKEN, "hello", now=T0)
        vg.record_typed(self.home, TOKEN, now=T0 + 1)
        state = vg.load_state(self.home, TOKEN)
        self.assertIsNotNone(vg.decide(state, "Write", {}, T0 + 2))
        vg.record_stopped(self.home, TOKEN, now=T0 + 3)
        vg.record_typed(self.home, TOKEN, now=T0 + 4)
        self.assertIsNone(vg.decide(vg.load_state(self.home, TOKEN), "Write", {}, T0 + 5))

    def test_stop_ends_grant_but_keeps_challenge(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern", now=T0)
        vg.record_voice(self.home, TOKEN, "confirm walnut lantern", now=T0 + 1)
        self.assertIsNone(vg.decide(vg.load_state(self.home, TOKEN), "Write", {}, T0 + 2))
        vg.record_stopped(self.home, TOKEN, now=T0 + 3)
        self.assertIsNotNone(vg.decide(vg.load_state(self.home, TOKEN), "Write", {}, T0 + 4))
        vg.record_confirm_request(self.home, TOKEN, "confirm apple banana", now=T0 + 5)
        vg.record_stopped(self.home, TOKEN, now=T0 + 6)
        self.assertTrue(vg.record_voice(self.home, TOKEN, "confirm apple banana", now=T0 + 7))

    def test_queued_nonmatching_voice_clears_grant(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern")
        vg.record_voice(self.home, TOKEN, "confirm walnut lantern")
        vg.record_voice(self.home, TOKEN, "queued request")
        self.assertDenied("Write")

    def test_typed_prompt_ungates(self):
        vg.record_voice(self.home, TOKEN, "make me a file")
        self.stop()
        rc, out, _ = self.hook("userpromptsubmit", {"prompt": "something I typed"})
        self.assertEqual((rc, out), (0, ""))
        self.assertAllowed("Write")

    def test_confirm_flow_grants_rest_of_turn(self):
        phrase = vg.record_confirm_request(self.home, TOKEN, "Say the wake word, then 'confirm walnut lantern'.", now=T0)
        self.assertEqual(phrase, "walnut lantern")
        self.assertTrue(vg.record_voice(self.home, TOKEN, "Confirm Walnut Lantern.", now=T0 + 5))
        state = vg.load_state(self.home, TOKEN)
        self.assertIsNone(vg.decide(state, "Write", {}, T0 + 6))
        self.assertIsNone(vg.decide(state, "Bash", {"command": "rm x"}, T0 + 6))
        self.assertFalse(vg.record_voice(self.home, TOKEN, "something else", now=T0 + 10))
        state = vg.load_state(self.home, TOKEN)
        self.assertIsNotNone(vg.decide(state, "Write", {}, T0 + 11))
        self.assertIsNotNone(vg.decide(state, "Bash", {"command": "rm x"}, T0 + 11))

    def test_confirm_via_hook_path_allows(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern")
        self.assertTrue(vg.record_voice(self.home, TOKEN, "confirm walnut lantern"))
        self.assertAllowed("Write")

    def test_wrong_phrase_then_right_phrase_no_grant(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern", now=T0)
        self.assertFalse(vg.record_voice(self.home, TOKEN, "confirm apple banana", now=T0 + 1))
        self.assertFalse(vg.record_voice(self.home, TOKEN, "confirm walnut lantern", now=T0 + 2))
        self.assertIsNotNone(vg.decide(vg.load_state(self.home, TOKEN), "Write", {}, T0 + 3))

    def test_expired_challenge_no_grant(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern", now=T0)
        self.assertFalse(vg.record_voice(self.home, TOKEN, "confirm walnut lantern", now=T0 + 301))

    def test_expired_grant_denied(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern", now=T0)
        vg.record_voice(self.home, TOKEN, "confirm walnut lantern", now=T0 + 1)
        state = vg.load_state(self.home, TOKEN)
        self.assertIsNone(vg.decide(state, "Write", {}, T0 + 2))
        self.assertIsNotNone(vg.decide(state, "Write", {}, T0 + 1 + 301))

    def test_typed_clears_grant(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern", now=T0)
        vg.record_voice(self.home, TOKEN, "confirm walnut lantern", now=T0 + 1)
        vg.record_stopped(self.home, TOKEN, now=T0 + 1.5)
        vg.record_typed(self.home, TOKEN, now=T0 + 2)
        state = vg.load_state(self.home, TOKEN)
        self.assertIsNone(state["grant"])
        self.assertIsNone(state["challenge"])
        self.assertIsNone(vg.decide(state, "Write", {}, T0 + 3))  # typed turn: not gated
        vg.record_voice(self.home, TOKEN, "do a thing", now=T0 + 4)
        self.assertIsNotNone(vg.decide(vg.load_state(self.home, TOKEN), "Write", {}, T0 + 5))


class ConfirmPhraseTests(GuardTestCase):
    def test_reuse_rejected(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern")
        with self.assertRaises(vg.ConfirmPhraseError) as cm:
            vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern")
        self.assertIn("already used", str(cm.exception))

    def test_no_phrase(self):
        with self.assertRaises(vg.ConfirmPhraseError) as cm:
            vg.record_confirm_request(self.home, TOKEN, "Shall I delete it?")
        self.assertIn("a confirm reply must ask", str(cm.exception))

    def test_stopword(self):
        with self.assertRaises(vg.ConfirmPhraseError):
            vg.record_confirm_request(self.home, TOKEN, "say confirm the deletion")

    def test_short_words(self):
        with self.assertRaises(vg.ConfirmPhraseError):
            vg.record_confirm_request(self.home, TOKEN, "confirm ox cart")

    def test_identical_words(self):
        with self.assertRaises(vg.ConfirmPhraseError):
            vg.record_confirm_request(self.home, TOKEN, "confirm walnut walnut")

    def test_last_valid_candidate_wins(self):
        text = "To delete scratch.txt, say the wake word, then 'confirm walnut lantern' to confirm the deletion."
        self.assertEqual(vg.record_confirm_request(self.home, TOKEN, text), "walnut lantern")

    def test_failed_request_leaves_no_challenge(self):
        try:
            vg.record_confirm_request(self.home, TOKEN, "nothing here")
        except vg.ConfirmPhraseError:
            pass
        state = vg.load_state(self.home, TOKEN)
        self.assertTrue(state is None or state["challenge"] is None)


class SubagentTaintTests(GuardTestCase):
    def agent(self, event, agent_id, token=TOKEN):
        return self.hook(event, {"agent_id": agent_id}, token=token)

    def pre_as(self, agent_id, tool, tool_input=None):
        return self.hook("pretooluse", {"tool_name": tool, "tool_input": tool_input or {},
                                        "agent_id": agent_id})

    def assertAgentDenied(self, agent_id, tool="Write"):
        rc, out, _ = self.pre_as(agent_id, tool)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def assertAgentAllowed(self, agent_id, tool="Write"):
        self.assertEqual(self.pre_as(agent_id, tool)[:2], (0, ""))

    def voice_then_typed_with_agent(self, agent_id="A"):
        vg.record_voice(self.home, TOKEN, "spawn a helper")
        self.assertEqual(self.agent("subagentstart", agent_id)[:2], (0, ""))
        self.stop()
        self.hook("userpromptsubmit", {"prompt": "typed now"})

    def test_voice_spawned_agent_stays_gated_after_typing(self):
        self.voice_then_typed_with_agent()
        self.assertAgentDenied("A")
        self.assertAllowed("Write")  # main thread, no agent_id

    def test_nested_child_after_typing_is_tainted(self):
        self.voice_then_typed_with_agent()
        self.agent("subagentstart", "B")
        self.assertAgentDenied("B")

    def test_missed_start_is_tainted(self):
        self.voice_then_typed_with_agent()
        self.assertAgentDenied("C")

    def test_early_stop_is_tainted(self):
        self.voice_then_typed_with_agent()
        self.agent("subagentstop", "A")
        self.assertAgentDenied("A")

    def test_invalid_agent_id_is_tainted(self):
        self.voice_then_typed_with_agent()
        for bad in ("x" * 200, "", 5, None, ["a"]):
            with self.subTest(bad=bad):
                self.assertAgentDenied(bad)
        # never a voice turn: nothing to taint
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        vg.record_typed(other, TOKEN)
        out = io.StringIO()
        rc = vg.main(["pretooluse", "--home", other, "--session", TOKEN],
                     stdin=io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {}, "agent_id": ""})),
                     stdout=out, stderr=io.StringIO())
        self.assertEqual((rc, out.getvalue()), (0, ""))

    def test_never_voice_session_allows_agents(self):
        vg.record_typed(self.home, TOKEN)
        self.assertAgentAllowed("Z")

    def test_typed_origin_agent_follows_main_logic(self):
        vg.record_typed(self.home, TOKEN)
        self.agent("subagentstart", "D")
        self.assertAgentAllowed("D")
        vg.record_voice(self.home, TOKEN, "now speak")
        self.assertAgentDenied("D")

    def test_live_taint_has_no_age_limit(self):
        vg.record_voice(self.home, TOKEN, "spawn", now=T0)
        vg.record_subagent_start(self.home, TOKEN, "A", now=T0 + 1)
        vg.record_stopped(self.home, TOKEN, now=T0 + 2)
        vg.record_typed(self.home, TOKEN, now=T0 + 3)
        vg.record_subagent_start(self.home, TOKEN, "E", now=T0 + 10_000)
        state = vg.load_state(self.home, TOKEN)
        self.assertTrue(state["agents"]["E"]["voice"])
        self.assertIsNotNone(vg.decide(state, "Write", {}, T0 + 10_001, "E"))

    def test_live_taint_applies_shortly_after(self):
        vg.record_voice(self.home, TOKEN, "spawn", now=T0)
        vg.record_subagent_start(self.home, TOKEN, "A", now=T0 + 1)
        vg.record_stopped(self.home, TOKEN, now=T0 + 2)
        vg.record_typed(self.home, TOKEN, now=T0 + 3)
        vg.record_subagent_start(self.home, TOKEN, "B", now=T0 + 100)
        self.assertTrue(vg.load_state(self.home, TOKEN)["agents"]["B"]["voice"])

    def test_eviction_prefers_typed_entries(self):
        vg.record_typed(self.home, TOKEN, now=T0)
        for i in range(vg.MAX_AGENTS - 1):
            vg.record_subagent_start(self.home, TOKEN, f"t{i}", now=T0 + 1)
        vg.record_voice(self.home, TOKEN, "speak", now=T0 + 2)
        vg.record_subagent_start(self.home, TOKEN, "v", now=T0 + 3)
        vg.record_subagent_start(self.home, TOKEN, "extra", now=T0 + 4)
        agents = vg.load_state(self.home, TOKEN)["agents"]
        self.assertEqual(len(agents), vg.MAX_AGENTS)
        self.assertIn("v", agents)
        self.assertNotIn("t0", agents)

    def test_inherited_grant_revoked_by_typing(self):
        vg.record_confirm_request(self.home, TOKEN, "confirm walnut lantern", now=T0)
        vg.record_voice(self.home, TOKEN, "confirm walnut lantern", now=T0 + 1)
        vg.record_subagent_start(self.home, TOKEN, "F", now=T0 + 2)
        vg.record_stopped(self.home, TOKEN, now=T0 + 3)
        state = vg.load_state(self.home, TOKEN)
        self.assertIsNone(vg.decide(state, "Write", {}, T0 + 4, "F"))
        self.assertIsNotNone(vg.decide(state, "Write", {}, T0 + 2 + 301, "F"))
        vg.record_typed(self.home, TOKEN, now=T0 + 5)
        state = vg.load_state(self.home, TOKEN)
        self.assertIsNotNone(vg.decide(state, "Write", {}, T0 + 6, "F"))

    def test_agent_hooks_always_exit_0_silently(self):
        for event in ("subagentstart", "subagentstop"):
            for payload, tok in (("garbage", TOKEN), ("{}", TOKEN), ({"agent_id": 5}, TOKEN),
                                 ({"agent_id": ""}, TOKEN), ({"agent_id": "x" * 200}, TOKEN),
                                 ({"agent_id": "a1"}, "badtoken")):
                with self.subTest(event=event, payload=payload, tok=tok):
                    self.assertEqual(self.hook(event, payload, token=tok)[:2], (0, ""))
            self.assertEqual(run_main([event], "{}")[:2], (0, ""))
        vg.record_armed(self.home, TOKEN)
        self.state_path().write_text("{not json")
        self.assertEqual(self.agent("subagentstart", "a1")[:2], (0, ""))

    def test_agents_capped(self):
        for i in range(vg.MAX_AGENTS + 5):
            vg.record_subagent_start(self.home, TOKEN, f"agent-{i}")
        agents = vg.load_state(self.home, TOKEN)["agents"]
        self.assertEqual(len(agents), vg.MAX_AGENTS)
        self.assertNotIn("agent-0", agents)
        self.assertIn(f"agent-{vg.MAX_AGENTS + 4}", agents)

    def test_malformed_agents_exit_2(self):
        vg.record_armed(self.home, TOKEN)
        for bad in ([], {"": {"voice": True, "granted_at": None, "started_at": 1}}, {"a": 1}, {"a": {"voice": "yes"}},
                    {"a": {"voice": True, "granted_at": "x", "started_at": 1}},
                    {"a": {"voice": True, "granted_at": None}},
                    {"a": {"voice": True, "granted_at": None, "started_at": float("nan")}}, 
                    {"x" * 129: {"voice": True, "granted_at": None, "started_at": 1}}):
            with self.subTest(bad=bad):
                self.state_path().write_text(json.dumps({"agents": bad}))
                self.assertEqual(self.pre("Read")[0], 2)

    def test_log_tags_agent_safely(self):
        vg.record_voice(self.home, TOKEN, "hi")
        self.pre_as("abc\ndef-ghijkl", "Write")
        log = (Path(self.home) / "logs" / "guard.log").read_text()
        self.assertIn(" a:abcdef-g", log)
        self.assertEqual(len([ln for ln in log.splitlines() if "pretooluse" in ln]), 1)


class FailClosedTests(GuardTestCase):
    def test_corrupt_state_exits_2(self):
        vg.record_armed(self.home, TOKEN)
        self.state_path().write_text("{not json")
        rc, out, err = self.pre("Write")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("failing closed", err)

    def test_wrong_types_exit_2(self):
        vg.record_armed(self.home, TOKEN)
        for bad in ({"voice_at": "yes"}, {"voice_at": True}, {"used_phrases": [1]},
                    {"grant": {"at": "x"}}, {"challenge": {"phrase": 1, "at": 1}}, []):
            with self.subTest(bad=bad):
                self.state_path().write_text(json.dumps(bad))
                self.assertEqual(self.pre("Read")[0], 2)

    def test_nan_voice_at_exits_2(self):
        vg.record_armed(self.home, TOKEN)
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                self.state_path().write_text(json.dumps({"voice_at": value}))
                self.assertEqual(self.pre("Write")[0], 2)

    def test_event_not_first_arg_still_exits_2(self):
        rc, _, _ = run_main(["--home", self.home, "pretooluse", "--session", "bad"], "garbage")
        self.assertEqual(rc, 2)
        for argv in ([], ["bogus"], ["--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv, "{}")[0], 2)

    def test_userpromptsubmit_failure_depends_on_marker(self):
        marked = json.dumps({"prompt": voice_prompt("hi")})
        for tok in ("badtoken", TOKEN):
            if tok == TOKEN:
                vg.record_armed(self.home, TOKEN)
                self.state_path().write_text("{not json")
            with self.subTest(tok=tok):
                rc, out, err = self.hook("userpromptsubmit", marked, token=tok)
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("voice turn dropped", err)
                for plain in (json.dumps({"prompt": "typed"}), "garbage"):
                    self.assertEqual(self.hook("userpromptsubmit", plain, token=tok)[:2], (0, ""))
        # raw marker with JSON-escaped quotes, unparseable as a whole
        raw = '{"prompt": "<channel source=\\"undertone\\" input=\\"voice\\">hi", broken'
        self.assertEqual(self.hook("userpromptsubmit", raw, token="badtoken")[0], 2)

    def test_symlinked_state_exits_2(self):
        vg.record_armed(self.home, TOKEN)
        target = Path(self.home) / "elsewhere.json"
        target.write_text("{}")
        self.state_path().unlink()
        self.state_path().symlink_to(target)
        self.assertEqual(self.pre("Write")[0], 2)

    def test_oversized_state_exits_2(self):
        vg.record_armed(self.home, TOKEN)
        self.state_path().write_text(json.dumps({"used_phrases": ["x" * 100] * 1000}))
        self.assertGreater(self.state_path().stat().st_size, vg.MAX_STATE_BYTES)
        self.assertEqual(self.pre("Write")[0], 2)

    def test_bad_token_exits_2(self):
        for tok in ("../etc/passwd", "ABC", TOKEN.upper(), ""):
            with self.subTest(tok=tok):
                rc, _, err = self.hook("pretooluse", {"tool_name": "Write"}, token=tok)
                self.assertEqual(rc, 2)
                self.assertIn("failing closed", err)

    def test_bad_stdin_exits_2(self):
        for payload in ("garbage", "", "[1]", "null", json.dumps({"tool_name": 3})):
            with self.subTest(payload=payload):
                self.assertEqual(self.hook("pretooluse", payload)[0], 2)

    def test_missing_args_pretooluse_exits_2(self):
        self.assertEqual(run_main(["pretooluse"], "{}")[0], 2)

    def test_unexpected_exception_exits_2(self):
        with mock.patch.object(vg, "decide", side_effect=RuntimeError("boom")):
            self.assertEqual(self.pre("Write")[0], 2)

    def test_stop_always_exits_0_silently(self):
        for payload, tok in (("garbage", TOKEN), ("{}", "badtoken"), ("", "badtoken")):
            with self.subTest(tok=tok):
                self.assertEqual(self.hook("stop", payload, token=tok)[:2], (0, ""))
        for argv in (["stop"], ["stop", "--home"], ["--home", str(Path(self.home) / "x"), "stop"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv, "{}")[:2], (0, ""))
        vg.record_armed(self.home, TOKEN)
        self.state_path().write_text("{not json")
        self.assertEqual(self.stop()[:2], (0, ""))

    def test_prompt_hooks_always_exit_0(self):
        cases = [
            ("userpromptsubmit", "garbage", TOKEN),
            ("userpromptsubmit", "{}", "badtoken"),
            ("sessionstart", "", "badtoken"),
            ("sessionstart", "garbage", TOKEN),
        ]
        for event, payload, tok in cases:
            with self.subTest(event=event, tok=tok):
                rc, out, _ = self.hook(event, payload, token=tok)
                self.assertEqual((rc, out), (0, ""))
        for argv in (["userpromptsubmit"], ["sessionstart", "--home"],
                     ["userpromptsubmit", "--home", str(Path(self.home) / "pretooluse"), "--session"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv, "{}")[0], 0)

    def test_prompt_hook_io_error_exit_0(self):
        vg.record_armed(self.home, TOKEN)
        self.state_path().write_text("{not json")
        self.assertEqual(self.hook("userpromptsubmit", {"prompt": "hi"})[0], 0)
        self.assertEqual(self.hook("sessionstart")[0], 0)

    def test_lock_timeout_raises(self):
        vg.record_armed(self.home, TOKEN)
        lock_fd = os.open(Path(self.home) / "sessions" / f"{TOKEN}.lock", os.O_RDWR)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with mock.patch.object(vg, "LOCK_TIMEOUT_S", 0.1):
            with self.assertRaises(vg.GuardError):
                vg.record_armed(self.home, TOKEN)


class PermissionAndLogTests(GuardTestCase):
    def mode(self, path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_modes(self):
        vg.record_voice(self.home, TOKEN, "hello there")
        self.pre("Write")
        self.assertEqual(self.mode(Path(self.home) / "sessions"), 0o700)
        self.assertEqual(self.mode(self.state_path()), 0o600)
        self.assertEqual(self.mode(Path(self.home) / "logs" / "guard.log"), 0o600)

    def test_log_never_holds_command_or_prompt_text(self):
        vg.record_voice(self.home, TOKEN, "my secret utterance")
        self.assertDenied("Bash", {"command": "rm secret-token-xyz"})
        self.hook("userpromptsubmit", {"prompt": "my secret utterance"})
        log = (Path(self.home) / "logs" / "guard.log").read_text()
        self.assertIn("deny", log)
        self.assertNotIn("secret-token-xyz", log)
        self.assertNotIn("secret utterance", log)


class SubprocessEntryTests(GuardTestCase):
    def test_module_entry_prints_deny(self):
        vg.record_voice(self.home, TOKEN, "write me a file")
        proc = subprocess.run(
            [sys.executable, "-P", "-m", "undertone.voice_guard", "pretooluse",
             "--home", self.home, "--session", TOKEN],
            input=json.dumps({"tool_name": "Write", "tool_input": {}}),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        decision = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()

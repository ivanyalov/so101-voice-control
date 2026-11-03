import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import grok_planner
import voice_toggle

Client = httpx.Client
ENV = {"XAI_API_KEY": "test-placeholder", "XAI_MODEL": "test-model", "XAI_REASONING_EFFORT": "none"}
PHRASE = "Hey, can you please move all the way to the right and then move your elbow up?"


def action(kind="jog", joint="elbow_flex", direction="up", amount=None, pose=None):
    return dict(type=kind, joint=joint, direction=direction, amount_degrees=amount, pose=pose)


def intent(actions=None, status="planned"):
    return {"status": status, "reply": "Move base right, then raise the elbow.", "actions": actions if actions is not None else [
        action("endpoint", "shoulder_pan", "right"), action(),
    ]}


def response(data):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}]}


class GrokPlannerTests(unittest.TestCase):
    def test_http_schema_to_intent_to_cyberwave_plan(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=response(intent()))

        with patch.dict(os.environ, ENV), patch.object(grok_planner.httpx, "Client", side_effect=lambda **kw: Client(transport=httpx.MockTransport(handle), **kw)):
            result = grok_planner.preview(PHRASE, mock_state=True)
        request = requests[0]
        body = json.loads(request.content)
        self.assertEqual(str(request.url), "https://api.x.ai/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer test-placeholder")
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["messages"][-1]["content"], PHRASE)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertNotIn("tools", body)
        self.assertEqual(result["status"], "resolved_mock")
        self.assertEqual(result["state_source"], "mock")
        self.assertEqual([(c["joint"], c["before"], c["target"], c["delta"]) for c in result["parameter_changes"]], [
            ("shoulder_pan", 15, 70, 55), ("elbow_flex", -20, -10, 10),
        ])
        self.assertEqual([(a["joint"], a["angle"]) for a in result["motion_plan"]["actions"]], [("1", 70), ("3", -10)])
        self.assertTrue(all(a["type"] == "set_joint" for a in result["motion_plan"]["actions"]))
        self.assertEqual(result["executed_actions"], 0)

    def test_no_state_retains_interpretation_without_numeric_targets(self):
        result = grok_planner.resolve(intent())
        self.assertEqual(result["status"], "needs_configuration")
        self.assertEqual(len(result["proposed_actions"]), 2)
        self.assertEqual(result["parameter_changes"], [])
        self.assertIsNone(result["motion_plan"])

    def test_sequential_moves_use_preceding_simulated_target(self):
        result = grok_planner.resolve(intent([action(), action()]), mock_state=True)
        self.assertEqual([(c["before"], c["target"]) for c in result["parameter_changes"]], [(-20, -10), (-10, 0)])

    def test_out_of_range_blocks_whole_plan_without_clamping(self):
        result = grok_planner.resolve(intent([
            action("endpoint", "shoulder_pan", "right"), action("relative", amount=100),
        ]), mock_state=True)
        self.assertEqual(result["status"], "needs_configuration")
        self.assertIsNone(result["motion_plan"])
        self.assertEqual(result["code_preview"], [])
        self.assertIn("exceed", result["unresolved"][0])

    def test_named_pose_is_not_replaced_with_shoulder_limit(self):
        result = grok_planner.resolve(intent([action("pose", None, None, pose="top")]), mock_state=True)
        self.assertIsNone(result["motion_plan"])
        self.assertEqual(result["parameter_changes"], [])
        self.assertIn("not configured", result["unresolved"][0])

    def test_stop_is_not_home(self):
        result = grok_planner.resolve(intent([action("stop", None, None)]), mock_state=True)
        self.assertEqual(result["status"], "preview_only")
        self.assertIsNone(result["motion_plan"])
        self.assertEqual(result["executed_actions"], 0)

    def test_invalid_llm_parameters_never_resolve(self):
        for actions in ([action("home")], [action(amount=30)],
                        [action("relative", amount=float("nan"))],
                        [action("relative", amount=True)],
                        [action(direction="right")],
                        [action("stop", None, None), action()]):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                grok_planner.resolve(intent(actions), mock_state=True)
        with self.assertRaises(ValueError):
            grok_planner.resolve(intent([action()], status="clarify"), mock_state=True)

    def test_clarification_never_produces_motion_plan(self):
        result = grok_planner.resolve(intent([], status="clarify"), mock_state=True)
        self.assertEqual(result["status"], "clarify")
        self.assertIsNone(result["motion_plan"])

    def test_incompatible_wrist_direction_requests_clarification_without_actions(self):
        bad = intent([action('relative', 'wrist_flex', 'right', 30)])
        with patch.dict(os.environ, ENV), patch.object(grok_planner.httpx, 'Client', side_effect=lambda **kw: Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response(bad))), **kw)):
            actual = grok_planner.interpret('Hey, move the wrist 30 degrees.')
        self.assertEqual(actual['status'], 'clarify')
        self.assertEqual(actual['actions'], [])
        self.assertIn('clockwise', actual['reply'])

    def test_missing_credentials_or_model_never_opens_http_client(self):
        for env in ({}, {"XAI_API_KEY": "test-placeholder"}):
            with patch.dict(os.environ, env, clear=True), patch.object(grok_planner.httpx, "Client") as client:
                with self.assertRaises(ValueError):
                    grok_planner.interpret(PHRASE)
                client.assert_not_called()

    def test_http_error_and_truncation_do_not_expose_server_body(self):
        for code, body in ((401, {"error": "sensitive-test-body"}), (200, {"choices": [{"finish_reason": "length", "message": {"content": "sensitive-test-body"}}]})):
            with patch.dict(os.environ, ENV), patch.object(grok_planner.httpx, "Client", side_effect=lambda **kw: Client(transport=httpx.MockTransport(lambda req: httpx.Response(code, json=body)), **kw)):
                with self.assertRaises((ValueError, RuntimeError)) as caught:
                    grok_planner.interpret(PHRASE)
                self.assertNotIn("sensitive-test-body", str(caught.exception))

    def test_voice_transcript_reaches_grok_adapter_and_resolver(self):
        recorder = Mock(recording=True)
        recorder.stop.return_value = Path("mock.wav")
        session = voice_toggle.Session(recorder, processor=lambda text: grok_planner.preview(text, mock_state=True))
        with patch.object(voice_toggle.readiness, "transcribe", return_value={"text": PHRASE}), \
             patch.object(grok_planner, "interpret", return_value=intent()) as llm, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            session.toggle()
            session.worker.join(timeout=2)
            recorder.recording = False
            session.poll()
        llm.assert_called_once_with(PHRASE)
        self.assertIn('"target": 70', output.getvalue())
        self.assertIn('"joint": "elbow_flex"', output.getvalue())
        self.assertIn('"state_source": "mock"', output.getvalue())
        self.assertIn('"executed_actions": 0', output.getvalue())

    def test_planner_failure_preserves_transcript_and_returns_ready(self):
        recorder = Mock(recording=True)
        recorder.stop.return_value = Path("mock.wav")
        session = voice_toggle.Session(recorder, processor=Mock(side_effect=RuntimeError("Grok unavailable")))
        with patch.object(voice_toggle.readiness, "transcribe", return_value={"text": PHRASE}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            session.toggle()
            session.worker.join(timeout=2)
            recorder.recording = False
            session.poll()
        self.assertIn(PHRASE, output.getvalue())
        self.assertIn('"status": "planner_failed"', output.getvalue())
        self.assertIn("READY", output.getvalue())
        self.assertFalse(session.busy)

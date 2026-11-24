"""Wave 3 统一运行器（selftest / live 预检 / report 重算 / 退出码）单元测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ..contract import (
    BudgetConfig,
    DecisionRecord,
    EndpointConfig,
    LiveConfig,
    sha256_text,
)
from ..run import execute_live, execute_report, execute_selftest, exit_code_for_state, main

_REPO_ROOT = Path(__file__).resolve().parents[3]


class RunOrchestratorTest(unittest.TestCase):
    def test_exit_code_contract(self) -> None:
        self.assertEqual(exit_code_for_state("OFFLINE_VERIFIED"), 0)
        self.assertEqual(exit_code_for_state("ELIGIBLE_B_AND_C"), 0)
        self.assertEqual(exit_code_for_state("ELIGIBLE_B_ONLY"), 3)
        self.assertEqual(exit_code_for_state("RETAIN_A"), 4)
        self.assertEqual(exit_code_for_state("INCOMPLETE"), 2)
        self.assertEqual(exit_code_for_state("HOLD"), 2)

    def test_selftest_twice_deterministic_and_zero_network_and_report_recomputes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pdftr-batch-d-run-") as tmp:
            out1 = Path(tmp) / "offline-1"
            out2 = Path(tmp) / "offline-2"
            d1, code1 = execute_selftest(
                repo_root=_REPO_ROOT,
                out_dir=out1,
                seed=20261125,
                run_id="selftest-unit",
                limit_docs=4,
                rounds=1,
            )
            d2, code2 = execute_selftest(
                repo_root=_REPO_ROOT,
                out_dir=out2,
                seed=20261125,
                run_id="selftest-unit",
                limit_docs=4,
                rounds=1,
            )
            self.assertEqual(code1, 0)
            self.assertEqual(code2, 0)
            self.assertEqual(d1.state, "OFFLINE_VERIFIED")
            self.assertEqual(d2.state, "OFFLINE_VERIFIED")
            self.assertFalse(d1.eligible_b)
            self.assertFalse(d1.eligible_c)
            self.assertEqual(d1.evidence_hash, d2.evidence_hash)

            manifest1 = json.loads((out1 / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest1["network_calls"], 0)
            self.assertNotEqual(
                manifest1["reviewers"]["codex"]["model_family"],
                manifest1["reviewers"]["neon"]["model_family"],
            )
            self.assertTrue((out1 / "blind" / "codex_r0.json").exists())
            self.assertTrue((out1 / "blind" / "neon_r0.json").exists())
            self.assertTrue((out1 / "private" / "map_codex_r0.json").exists())
            self.assertTrue((out1 / "report.md").exists())

            # 盲包内不能出现私有映射字段
            blind_text = (out1 / "blind" / "codex_r0.json").read_text(encoding="utf-8")
            self.assertNotIn("label_to_groups", blind_text)
            self.assertNotIn("permutation", blind_text)

            # report 无网络重算保持相同状态与 evidence_hash
            d_rep, code_rep = execute_report(repo_root=_REPO_ROOT, run_dir=out1)
            self.assertEqual(code_rep, 0)
            self.assertEqual(d_rep.state, "OFFLINE_VERIFIED")
            self.assertEqual(d_rep.evidence_hash, d1.evidence_hash)

    def test_live_preflight_blocks_missing_credentials_without_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pdftr-batch-d-live-") as tmp:
            cfg_path = Path(tmp) / "live-config.json"
            out_dir = Path(tmp) / "live-out"
            cfg = LiveConfig(
                generator=EndpointConfig(
                    protocol="openai_compat",
                    base_url="https://api.example.invalid/v1",
                    model="deepseek-chat",
                    model_family="deepseek",
                    key_env="PDFTR_MISSING_TEST_GEN_KEY",
                ),
                codex=EndpointConfig(
                    protocol="openai_compat",
                    base_url="https://api.example.invalid/v1",
                    model="gpt-judge",
                    model_family="openai",
                    key_env="PDFTR_MISSING_TEST_CODEX_KEY",
                ),
                neon=EndpointConfig(
                    protocol="anthropic",
                    base_url="https://api.example.invalid",
                    model="gemini-judge",
                    model_family="gemini",
                    key_env="PDFTR_MISSING_TEST_NEON_KEY",
                ),
                budget=BudgetConfig(max_requests=2000, max_cost_cny=50.0, max_seconds_per_stage=600.0),
            )
            for key in (cfg.generator.key_env, cfg.codex.key_env, cfg.neon.key_env):
                os.environ.pop(key, None)
            cfg_path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")

            decision, code = execute_live(repo_root=_REPO_ROOT, config_path=cfg_path, out_dir=out_dir)
            self.assertEqual(code, 2)
            self.assertEqual(decision.state, "INCOMPLETE")
            self.assertFalse(decision.eligible_b)
            self.assertFalse(decision.eligible_c)
            self.assertTrue(any("PDFTR_MISSING_TEST_GEN_KEY" in r for r in decision.reasons))
            self.assertTrue((out_dir / "decision.json").exists())

            # CLI 入口同步返回退出码 2
            cli_code = main(["live", "--config", str(cfg_path), "--out", str(out_dir)], repo_root=_REPO_ROOT)
            self.assertEqual(cli_code, 2)

    def test_out_dir_inside_repo_is_rejected(self) -> None:
        inside = _REPO_ROOT / "spikes" / "batch_d" / "_forbidden_out"
        code = main(["selftest", "--out", str(inside)], repo_root=_REPO_ROOT)
        self.assertEqual(code, 1)
        self.assertFalse(inside.exists())
        # 确认 DecisionRecord 校验与 sha256 工具正常工作
        rec = DecisionRecord(
            run_id="check",
            mode="selftest",
            state="OFFLINE_VERIFIED",
            eligible_b=False,
            eligible_c=False,
            evidence_hash=sha256_text("ok"),
        )
        self.assertEqual(rec.state, "OFFLINE_VERIFIED")


if __name__ == "__main__":
    unittest.main()

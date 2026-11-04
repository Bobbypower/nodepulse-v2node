"""Keep test-branch runtime builds from changing the shared rolling release."""
from pathlib import Path
import re
import unittest


class ReleaseIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/build.yml").read_text(encoding="utf-8")

    def step(self, name):
        match = re.search(rf"(?ms)^      - name: {re.escape(name)}\n(.*?)(?=^      - name: |\Z)", self.workflow)
        self.assertIsNotNone(match, f"missing workflow step: {name}")
        return match.group(1)

    def test_only_main_can_move_or_publish_the_shared_rolling_release(self):
        for name in ("Move rolling release tag", "Publish rolling release"):
            with self.subTest(name=name):
                step = self.step(name)
                self.assertEqual(len(re.findall(r"(?m)^        if:", step)), 1)
                self.assertRegex(step, r"(?m)^        if: github\.ref == 'refs/heads/main'$")

    def test_test_branch_retains_the_immutable_non_latest_release(self):
        step = self.step("Publish immutable bootstrap source")
        self.assertIn("tag_name: nodepulse-${{ github.sha }}", step)
        self.assertIn("prerelease: true", step)
        self.assertIn("make_latest: false", step)
        self.assertNotIn("refs/heads/main", step)


if __name__ == "__main__":
    unittest.main()
